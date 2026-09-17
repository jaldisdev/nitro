//
// This source file is part of the Nitro open source project.
//
// Copyright (c) 2026 Jaldis B.V.
//
// Licensed under the MIT OR Apache-2.0 license (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://opensource.org/licenses/MIT
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//

//! The compiled route table.
//!
//! Routes are kept in a tree of path segments. A segment is either text, which
//! is looked up directly, or an expression built from a route's literal text
//! and its parameters' expressions, which is tried against the segment. A route
//! ending in a parameter that spans separators keeps an expression for the rest
//! of the path instead.
//!
//! At each level the text branch is tried first, then the expression branches
//! and the tails in registration order. A branch that does not lead to a route
//! is left for the next, so an expression that rejects a segment never hides a
//! route registered after it. The first route whose path matches and which
//! answers the method wins.

use std::collections::{BTreeSet, HashMap};
use std::ops::ControlFlow;

use crate::router::route::{
    Compilation, CompiledRoute, Pattern, RouteDefinition, RouteError, Segment, compile,
};

#[derive(Debug, thiserror::Error)]
pub enum RouterError {
    #[error(transparent)]
    Route(#[from] RouteError),
}

/// What a path lookup found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RouteMatch {
    Found {
        route_id: u64,
        parameters: Vec<(String, String)>,
    },
    /// The path is known but not for this method. The allowed methods are
    /// listed so the caller can say so in an `Allow` header.
    MethodNotAllowed {
        allowed: Vec<String>,
    },
    NotFound,
}

#[derive(Debug, Default)]
struct Node {
    texts: HashMap<String, Node>,
    /// In the order their first route was registered.
    patterns: Vec<(Pattern, Node)>,
    /// Routes matched by an expression over the rest of the path, as indices
    /// into the table's routes.
    tails: Vec<(Pattern, usize)>,
    /// Routes whose path ends at this node.
    routes: Vec<usize>,
}

#[derive(Debug, Default)]
pub struct RouteTable {
    root: Node,
    routes: Vec<CompiledRoute>,
    /// The declared path of every route, by identifier. Metric labels need the
    /// pattern rather than the requested path, and a match only reports the
    /// identifier.
    declared: HashMap<u64, String>,
}

impl RouteTable {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn build(
        definitions: impl IntoIterator<Item = RouteDefinition>,
    ) -> Result<Self, RouterError> {
        let mut table = Self::new();
        for definition in definitions {
            table.insert(definition)?;
        }
        Ok(table)
    }

    /// Add a route.
    pub fn insert(&mut self, definition: RouteDefinition) -> Result<(), RouterError> {
        let Compilation {
            segments,
            tail,
            route,
        } = compile(&definition)?;
        let index = self.routes.len();

        let mut node = &mut self.root;
        for segment in segments {
            node = match segment {
                Segment::Static(text) => node.texts.entry(text).or_default(),
                Segment::Pattern(pattern) => {
                    let position = match node
                        .patterns
                        .iter()
                        .position(|(existing, _)| existing.source == pattern.source)
                    {
                        Some(position) => position,
                        None => {
                            node.patterns.push((pattern, Node::default()));
                            node.patterns.len() - 1
                        }
                    };
                    &mut node.patterns[position].1
                }
            };
        }
        match tail {
            Some(pattern) => node.tails.push((pattern, index)),
            None => node.routes.push(index),
        }

        self.declared.insert(route.id, definition.path);
        self.routes.push(route);
        Ok(())
    }

    /// Find the route that answers `method` for `path`.
    pub fn find(&self, method: &str, path: &str) -> RouteMatch {
        let segments: Vec<(usize, &str)> = path
            .split('/')
            .scan(0_usize, |offset, segment| {
                let start = *offset;
                *offset += segment.len() + 1;
                Some((start, segment))
            })
            .collect();

        let mut allowed: BTreeSet<String> = BTreeSet::new();
        let mut captures: Vec<&str> = Vec::new();
        let mut search = Search {
            path,
            segments: &segments,
            routes: &self.routes,
        };

        let found = search.visit(&self.root, 0, &mut captures, &mut |route, values| {
            if route.accepts(method) {
                return ControlFlow::Break(RouteMatch::Found {
                    route_id: route.id,
                    parameters: route
                        .parameters
                        .iter()
                        .cloned()
                        .zip(values.iter().map(|value| (*value).to_owned()))
                        .collect(),
                });
            }
            allowed.extend(route.methods.iter().cloned());
            ControlFlow::Continue(())
        });

        if let ControlFlow::Break(found) = found {
            return found;
        }
        if allowed.is_empty() {
            return RouteMatch::NotFound;
        }
        // A route that answers GET answers HEAD, and advertising that keeps the
        // two consistent with how they are matched.
        if allowed.contains("GET") {
            allowed.insert("HEAD".to_owned());
        }
        RouteMatch::MethodNotAllowed {
            allowed: allowed.into_iter().collect(),
        }
    }

    /// The path a route was declared with, such as `/users/<int:id>`.
    pub fn declared_path(&self, route_id: u64) -> Option<&str> {
        self.declared.get(&route_id).map(String::as_str)
    }

    /// The number of routes registered.
    pub fn len(&self) -> usize {
        self.routes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.routes.is_empty()
    }
}

struct Search<'a> {
    path: &'a str,
    /// Each segment of the path with the offset it starts at.
    segments: &'a [(usize, &'a str)],
    routes: &'a [CompiledRoute],
}

impl<'a> Search<'a> {
    /// Offer every route whose path matches to `offer`, most specific branch
    /// first, until it breaks.
    fn visit<B>(
        &mut self,
        node: &Node,
        depth: usize,
        captures: &mut Vec<&'a str>,
        offer: &mut impl FnMut(&CompiledRoute, &[&str]) -> ControlFlow<B>,
    ) -> ControlFlow<B> {
        let Some(&(offset, segment)) = self.segments.get(depth) else {
            for index in &node.routes {
                if let Some(route) = self.routes.get(*index) {
                    offer(route, captures)?;
                }
            }
            return ControlFlow::Continue(());
        };

        if let Some(child) = node.texts.get(segment) {
            self.visit(child, depth + 1, captures, offer)?;
        }

        for (pattern, child) in &node.patterns {
            let mark = captures.len();
            if capture(pattern, segment, captures) {
                self.visit(child, depth + 1, captures, offer)?;
            }
            captures.truncate(mark);
        }

        let rest = self.path.get(offset..).unwrap_or_default();
        for (pattern, index) in &node.tails {
            let mark = captures.len();
            if capture(pattern, rest, captures)
                && let Some(route) = self.routes.get(*index)
            {
                offer(route, captures)?;
            }
            captures.truncate(mark);
        }

        ControlFlow::Continue(())
    }
}

/// Match `text` against `pattern`, appending what its parameters captured.
fn capture<'a>(pattern: &Pattern, text: &'a str, captures: &mut Vec<&'a str>) -> bool {
    let Some(found) = pattern.expression.captures(text) else {
        return false;
    };

    let mark = captures.len();
    for (group, spans_separators) in pattern.groups.iter().zip(&pattern.spans_separators) {
        let value = found.get(*group).map_or("", |value| value.as_str());
        if !spans_separators && value.contains('/') {
            captures.truncate(mark);
            return false;
        }
        captures.push(value);
    }
    true
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::router::route::ParameterSpec;

    fn route(
        id: u64,
        path: &str,
        methods: &[&str],
        parameters: Vec<ParameterSpec>,
    ) -> RouteDefinition {
        RouteDefinition::new(id, path, methods.iter().map(|method| (*method).to_owned()))
            .with_parameters(parameters)
    }

    fn found(table: &RouteTable, method: &str, path: &str) -> (u64, Vec<(String, String)>) {
        match table.find(method, path) {
            RouteMatch::Found {
                route_id,
                parameters,
            } => (route_id, parameters),
            other => panic!("expected a match for {method} {path}, got {other:?}"),
        }
    }

    #[test]
    fn a_static_route_matches() {
        let table = RouteTable::build([route(1, "/about", &["GET"], Vec::new())]).unwrap();
        assert_eq!(found(&table, "GET", "/about").0, 1);
        assert_eq!(table.find("GET", "/elsewhere"), RouteMatch::NotFound);
    }

    #[test]
    fn a_parameter_is_captured_under_its_own_name() {
        let table = RouteTable::build([route(
            1,
            "/users/<int:identifier>",
            &["GET"],
            vec![ParameterSpec::new("identifier", "[0-9]+")],
        )])
        .unwrap();

        let (id, parameters) = found(&table, "GET", "/users/42");
        assert_eq!(id, 1);
        assert_eq!(parameters, vec![("identifier".to_owned(), "42".to_owned())]);
    }

    #[test]
    fn several_parameters_are_captured_in_order() {
        let table = RouteTable::build([route(
            1,
            "/users/<int:identifier>/posts/<slug:title>",
            &["GET"],
            vec![
                ParameterSpec::new("identifier", "[0-9]+"),
                ParameterSpec::new("title", "[-a-z0-9]+"),
            ],
        )])
        .unwrap();

        let (_, parameters) = found(&table, "GET", "/users/7/posts/hello-world");
        assert_eq!(
            parameters,
            vec![
                ("identifier".to_owned(), "7".to_owned()),
                ("title".to_owned(), "hello-world".to_owned()),
            ]
        );
    }

    #[test]
    fn a_value_the_expression_rejects_does_not_match() {
        let table = RouteTable::build([route(
            1,
            "/users/<int:identifier>",
            &["GET"],
            vec![ParameterSpec::new("identifier", "[0-9]+")],
        )])
        .unwrap();

        assert_eq!(table.find("GET", "/users/abc"), RouteMatch::NotFound);
    }

    #[test]
    fn routes_of_the_same_shape_are_told_apart_by_what_they_accept() {
        let table = RouteTable::build([
            route(
                1,
                "/things/<int:identifier>",
                &["GET"],
                vec![ParameterSpec::new("identifier", "[0-9]+")],
            ),
            route(
                2,
                "/things/<slug:name>",
                &["GET"],
                vec![ParameterSpec::new("name", "[-a-z]+")],
            ),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/things/42").0, 1);
        assert_eq!(found(&table, "GET", "/things/some-name").0, 2);
        assert_eq!(table.find("GET", "/things/UPPER"), RouteMatch::NotFound);
    }

    #[test]
    fn registration_order_decides_between_overlapping_routes() {
        let table = RouteTable::build([
            route(
                1,
                "/things/<str:anything>",
                &["GET"],
                vec![ParameterSpec::new("anything", "[^/]+")],
            ),
            route(
                2,
                "/things/<int:identifier>",
                &["GET"],
                vec![ParameterSpec::new("identifier", "[0-9]+")],
            ),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/things/42").0, 1);
    }

    #[test]
    fn a_static_segment_wins_over_a_parameter() {
        let table = RouteTable::build([
            route(
                1,
                "/users/<str:name>",
                &["GET"],
                vec![ParameterSpec::new("name", "[^/]+")],
            ),
            route(2, "/users/new", &["GET"], Vec::new()),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/users/new").0, 2);
        assert_eq!(found(&table, "GET", "/users/ada").0, 1);
    }

    #[test]
    fn a_greedy_parameter_spans_separators() {
        let table = RouteTable::build([route(
            1,
            "/files/<path:rest>",
            &["GET"],
            vec![ParameterSpec::new("rest", ".+").greedy()],
        )])
        .unwrap();

        let (_, parameters) = found(&table, "GET", "/files/deep/nested/file.txt");
        assert_eq!(
            parameters,
            vec![("rest".to_owned(), "deep/nested/file.txt".to_owned())]
        );
    }

    #[test]
    fn a_non_greedy_parameter_stops_at_a_separator() {
        let table = RouteTable::build([route(
            1,
            "/files/<str:name>",
            &["GET"],
            vec![ParameterSpec::new("name", "[^/]+")],
        )])
        .unwrap();

        assert_eq!(
            table.find("GET", "/files/deep/nested"),
            RouteMatch::NotFound
        );
    }

    #[test]
    fn a_known_path_with_the_wrong_method_reports_what_is_allowed() {
        let table = RouteTable::build([
            route(1, "/things", &["POST"], Vec::new()),
            route(2, "/things", &["PUT"], Vec::new()),
        ])
        .unwrap();

        assert_eq!(
            table.find("DELETE", "/things"),
            RouteMatch::MethodNotAllowed {
                allowed: vec!["POST".to_owned(), "PUT".to_owned()]
            }
        );
    }

    #[test]
    fn head_is_answered_and_advertised_alongside_get() {
        let table = RouteTable::build([route(1, "/page", &["GET"], Vec::new())]).unwrap();

        assert_eq!(found(&table, "HEAD", "/page").0, 1);
        assert_eq!(
            table.find("DELETE", "/page"),
            RouteMatch::MethodNotAllowed {
                allowed: vec!["GET".to_owned(), "HEAD".to_owned()]
            }
        );
    }

    #[test]
    fn the_same_path_can_carry_different_methods() {
        let table = RouteTable::build([
            route(1, "/things", &["GET"], Vec::new()),
            route(2, "/things", &["POST"], Vec::new()),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/things").0, 1);
        assert_eq!(found(&table, "POST", "/things").0, 2);
    }

    #[test]
    fn a_parameterised_path_can_carry_different_methods() {
        let parameters = || vec![ParameterSpec::new("identifier", "[0-9]+")];
        let table = RouteTable::build([
            route(1, "/items/<int:identifier>", &["GET"], parameters()),
            route(2, "/items/<int:identifier>", &["DELETE"], parameters()),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/items/3").0, 1);
        assert_eq!(found(&table, "DELETE", "/items/3").0, 2);
    }

    #[test]
    fn a_trailing_slash_is_a_different_path() {
        let table = RouteTable::build([route(1, "/things/", &["GET"], Vec::new())]).unwrap();

        assert_eq!(found(&table, "GET", "/things/").0, 1);
        assert_eq!(table.find("GET", "/things"), RouteMatch::NotFound);
    }

    #[test]
    fn a_segment_can_hold_several_parameters_and_text() {
        let table = RouteTable::build([route(
            1,
            "/media/photo_<id><size>.<extension>",
            &["GET"],
            vec![
                ParameterSpec::new("id", "[0-9]+"),
                ParameterSpec::new("size", "(?:_[0-9]+x[0-9]+)?"),
                ParameterSpec::new("extension", "jpg|png"),
            ],
        )])
        .unwrap();

        let (_, parameters) = found(&table, "GET", "/media/photo_42_64x64.png");
        assert_eq!(
            parameters,
            vec![
                ("id".to_owned(), "42".to_owned()),
                ("size".to_owned(), "_64x64".to_owned()),
                ("extension".to_owned(), "png".to_owned()),
            ]
        );
        let (_, parameters) = found(&table, "GET", "/media/photo_42.jpg");
        assert_eq!(parameters[1], ("size".to_owned(), String::new()));
        assert_eq!(
            table.find("GET", "/media/photo_42.gif"),
            RouteMatch::NotFound
        );
    }

    #[test]
    fn a_prefixed_parameter_and_a_bare_one_can_share_a_position() {
        let parameters = |names: &[&str]| {
            names
                .iter()
                .map(|name| ParameterSpec::new(*name, "[0-9a-z]+"))
                .collect()
        };
        let table = RouteTable::build([
            route(
                1,
                "/contents/<owner>/file_<file>",
                &["GET"],
                parameters(&["owner", "file"]),
            ),
            route(
                2,
                "/contents/<owner>/<container>/file_<file>",
                &["GET"],
                parameters(&["owner", "container", "file"]),
            ),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/contents/ada/file_one").0, 1);
        let (id, parameters) = found(&table, "GET", "/contents/ada/box/file_one");
        assert_eq!(id, 2);
        assert_eq!(parameters[1], ("container".to_owned(), "box".to_owned()));
    }

    #[test]
    fn a_rejected_segment_falls_through_to_the_next_branch() {
        let table = RouteTable::build([
            route(
                1,
                "/things/<int:identifier>/detail",
                &["GET"],
                vec![ParameterSpec::new("identifier", "[0-9]+")],
            ),
            route(
                2,
                "/things/<str:name>/<str:part>",
                &["GET"],
                vec![
                    ParameterSpec::new("name", "[^/]+"),
                    ParameterSpec::new("part", "[^/]+"),
                ],
            ),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/things/42/detail").0, 1);
        assert_eq!(found(&table, "GET", "/things/42/other").0, 2);
        assert_eq!(found(&table, "GET", "/things/ada/detail").0, 2);
    }

    #[test]
    fn a_route_answering_the_method_wins_over_a_closer_one_that_does_not() {
        let table = RouteTable::build([
            route(1, "/users/new", &["POST"], Vec::new()),
            route(
                2,
                "/users/<str:name>",
                &["GET"],
                vec![ParameterSpec::new("name", "[^/]+")],
            ),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/users/new").0, 2);
        assert_eq!(found(&table, "POST", "/users/new").0, 1);
        assert_eq!(
            table.find("DELETE", "/users/new"),
            RouteMatch::MethodNotAllowed {
                allowed: vec!["GET".to_owned(), "HEAD".to_owned(), "POST".to_owned()]
            }
        );
    }

    #[test]
    fn a_tail_can_follow_text_in_its_segment() {
        let table = RouteTable::build([route(
            1,
            "/files/archive_<path:rest>",
            &["GET"],
            vec![ParameterSpec::new("rest", ".+").greedy()],
        )])
        .unwrap();

        let (_, parameters) = found(&table, "GET", "/files/archive_2026/09/log.txt");
        assert_eq!(
            parameters,
            vec![("rest".to_owned(), "2026/09/log.txt".to_owned())]
        );
        assert_eq!(
            table.find("GET", "/files/other/log.txt"),
            RouteMatch::NotFound
        );
    }

    #[test]
    fn a_parameter_before_a_tail_still_stops_at_a_separator() {
        let table = RouteTable::build([route(
            1,
            "/files/<name>-<path:rest>",
            &["GET"],
            vec![
                ParameterSpec::new("name", ".+"),
                ParameterSpec::new("rest", ".+").greedy(),
            ],
        )])
        .unwrap();

        let (_, parameters) = found(&table, "GET", "/files/report-2026/09");
        assert_eq!(parameters[0], ("name".to_owned(), "report".to_owned()));
        assert_eq!(
            table.find("GET", "/files/deep/report-2026"),
            RouteMatch::NotFound
        );
    }

    #[test]
    fn a_static_route_and_a_tail_at_the_same_place_coexist() {
        let table = RouteTable::build([
            route(
                1,
                "/files/<path:rest>",
                &["GET"],
                vec![ParameterSpec::new("rest", ".+").greedy()],
            ),
            route(2, "/files/index", &["GET"], Vec::new()),
        ])
        .unwrap();

        assert_eq!(found(&table, "GET", "/files/index").0, 2);
        assert_eq!(found(&table, "GET", "/files/index/more").0, 1);
    }

    #[test]
    fn an_empty_table_finds_nothing() {
        let table = RouteTable::new();
        assert_eq!(table.find("GET", "/"), RouteMatch::NotFound);
    }

    #[test]
    fn a_broken_route_is_reported_at_build_time() {
        let error = RouteTable::build([route(1, "/users/<int:identifier", &["GET"], Vec::new())])
            .unwrap_err();
        assert!(matches!(error, RouterError::Route(_)));
    }
}
