//! The compiled route table.
//!
//! Matching happens in two steps. A radix tree finds the routes whose shape
//! fits the path, which is the part that has to be fast because it runs on
//! every request. The candidates it returns are then checked against what each
//! of their parameters actually accepts, in registration order, and the first
//! that passes wins.
//!
//! Splitting it this way is what lets two routes with the same shape but
//! different parameter types coexist: the tree sees one entry, and the
//! expressions tell them apart.

use std::collections::{BTreeSet, HashMap};

use crate::router::route::{CompiledRoute, RouteDefinition, RouteError, compile};

#[derive(Debug, thiserror::Error)]
pub enum RouterError {
    #[error(transparent)]
    Route(#[from] RouteError),
    #[error("route {path:?} cannot be added: {reason}")]
    Conflict { path: String, reason: String },
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
pub struct RouteTable {
    tree: matchit::Router<Vec<CompiledRoute>>,
    /// Templates already in the tree. The tree cannot be asked whether it holds
    /// a template — only whether it matches a path — and those are different
    /// questions: `/users/new` is matched by `/users/{p0}` but is not that
    /// template, and treating it as one would file the static route behind the
    /// parameterised one where it could never win.
    templates: BTreeSet<String>,
    /// The declared path of every route, by identifier. Metric labels need the
    /// pattern rather than the requested path, and a match only reports the
    /// identifier.
    declared: HashMap<u64, String>,
    count: usize,
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
    ///
    /// Routes that compile to the same template are kept together, in the order
    /// they were added, and tried in that order at match time.
    pub fn insert(&mut self, definition: RouteDefinition) -> Result<(), RouterError> {
        let compilation = compile(&definition)?;
        let template = compilation.template;

        if self.templates.contains(&template) {
            let probe = probe_path(&template);
            let found = self
                .tree
                .at_mut(&probe)
                .map_err(|error| RouterError::Conflict {
                    path: definition.path.clone(),
                    reason: error.to_string(),
                })?;
            self.declared
                .insert(compilation.route.id, definition.path.clone());
            found.value.push(compilation.route);
            self.count += 1;
            return Ok(());
        }

        self.declared
            .insert(compilation.route.id, definition.path.clone());
        self.tree
            .insert(&template, vec![compilation.route])
            .map_err(|error| RouterError::Conflict {
                path: definition.path.clone(),
                reason: error.to_string(),
            })?;
        self.templates.insert(template);
        self.count += 1;
        Ok(())
    }

    /// Find the route that answers `method` for `path`.
    pub fn find(&self, method: &str, path: &str) -> RouteMatch {
        let Ok(found) = self.tree.at(path) else {
            return RouteMatch::NotFound;
        };

        let mut allowed: BTreeSet<String> = BTreeSet::new();
        for route in found.value {
            let Some(parameters) = capture(route, &found.params) else {
                continue;
            };
            if route.accepts(method) {
                return RouteMatch::Found {
                    route_id: route.id,
                    parameters,
                };
            }
            allowed.extend(route.methods.iter().cloned());
        }

        if allowed.is_empty() {
            RouteMatch::NotFound
        } else {
            // A route that answers GET answers HEAD, and advertising that keeps
            // the two consistent with how they are matched.
            if allowed.contains("GET") {
                allowed.insert("HEAD".to_owned());
            }
            RouteMatch::MethodNotAllowed {
                allowed: allowed.into_iter().collect(),
            }
        }
    }

    /// The path a route was declared with, such as `/users/<int:id>`.
    pub fn declared_path(&self, route_id: u64) -> Option<&str> {
        self.declared.get(&route_id).map(String::as_str)
    }

    /// The number of routes registered.
    pub fn len(&self) -> usize {
        self.count
    }

    pub fn is_empty(&self) -> bool {
        self.count == 0
    }
}

/// Check a candidate's parameters against the captured values.
fn capture(
    route: &CompiledRoute,
    captured: &matchit::Params<'_, '_>,
) -> Option<Vec<(String, String)>> {
    let mut parameters = Vec::with_capacity(route.parameters.len());

    for (position, parameter) in route.parameters.iter().enumerate() {
        let value = captured.get(format!("p{position}"))?;
        if !parameter.expression.is_match(value) {
            return None;
        }
        parameters.push((parameter.name.clone(), value.to_owned()));
    }

    Some(parameters)
}

/// A concrete path that reaches `template`, used to find an entry that is
/// already in the tree. Placeholders are filled with text that any expression
/// check will be applied to separately.
fn probe_path(template: &str) -> String {
    let mut path = String::with_capacity(template.len());
    let mut rest = template;

    while let Some(open) = rest.find('{') {
        path.push_str(&rest[..open]);
        let after = &rest[open + 1..];
        match after.find('}') {
            Some(close) => {
                path.push('\u{1}');
                rest = &after[close + 1..];
            }
            None => {
                rest = after;
                break;
            }
        }
    }
    path.push_str(rest);
    path
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
