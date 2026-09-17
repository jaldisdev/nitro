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

//! Route definitions and the compilation of a path pattern.
//!
//! A pattern is written as literal text with parameters in angle brackets, for
//! example `/users/<int:identifier>/posts/<slug:title>`. What sits before the
//! parameter name selects how the value is recognised and converted, and that
//! selection is made outside this crate: the caller supplies each parameter's
//! name, the expression that recognises it, and whether it may span path
//! separators. Nothing here knows what an `int` or a `slug` is.

use std::collections::BTreeSet;

use regex::Regex;

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum RouteError {
    #[error("route {path:?}: '<' at position {position} is never closed")]
    UnclosedParameter { path: String, position: usize },
    #[error("route {path:?}: '>' at position {position} has no opening '<'")]
    UnopenedParameter { path: String, position: usize },
    #[error("route {path:?}: parameter {name:?} was not described by the caller")]
    UndescribedParameter { path: String, name: String },
    #[error("route {path:?}: parameter {name:?} appears more than once")]
    DuplicateParameter { path: String, name: String },
    #[error("route {path:?}: parameter {name:?} has an empty name")]
    EmptyParameterName { path: String, name: String },
    #[error("route {path:?}: the expression for parameter {name:?} is unusable: {reason}")]
    UnusableExpression {
        path: String,
        name: String,
        reason: String,
    },
    #[error("route {path:?}: a parameter that spans separators must be the last thing in the path")]
    GreedyParameterNotLast { path: String },
}

/// How a single parameter is recognised.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParameterSpec {
    pub name: String,
    /// An expression the captured text must match in full.
    pub pattern: String,
    /// Whether the parameter may span `/`, as a trailing catch-all does.
    pub greedy: bool,
}

impl ParameterSpec {
    pub fn new(name: impl Into<String>, pattern: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            pattern: pattern.into(),
            greedy: false,
        }
    }

    pub fn greedy(mut self) -> Self {
        self.greedy = true;
        self
    }
}

/// A route as the caller describes it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteDefinition {
    pub id: u64,
    pub path: String,
    pub methods: Vec<String>,
    pub parameters: Vec<ParameterSpec>,
}

impl RouteDefinition {
    pub fn new(
        id: u64,
        path: impl Into<String>,
        methods: impl IntoIterator<Item = String>,
    ) -> Self {
        Self {
            id,
            path: path.into(),
            methods: methods.into_iter().collect(),
            parameters: Vec::new(),
        }
    }

    pub fn with_parameters(mut self, parameters: Vec<ParameterSpec>) -> Self {
        self.parameters = parameters;
        self
    }
}

/// A route ready to be matched against.
#[derive(Debug)]
pub struct CompiledRoute {
    pub id: u64,
    pub path: String,
    pub methods: BTreeSet<String>,
    /// Parameter names in the order they appear in the path, which is the order
    /// a match captures their values in.
    pub parameters: Vec<String>,
}

impl CompiledRoute {
    /// Whether `method` is one this route answers.
    ///
    /// `HEAD` falls back to `GET`: a response to `HEAD` is a response to `GET`
    /// with the body left off, so a route that answers one answers the other.
    pub fn accepts(&self, method: &str) -> bool {
        self.methods.contains(method) || (method == "HEAD" && self.methods.contains("GET"))
    }
}

/// An expression over part of a path, and where its parameters are captured.
#[derive(Debug, Clone)]
pub(crate) struct Pattern {
    /// The expression's source, which is what identifies it: two routes whose
    /// segments compile to the same source share one branch of the tree.
    pub source: String,
    pub expression: Regex,
    /// The capture group of each parameter, in path order.
    pub groups: Vec<usize>,
    /// Which of those parameters may contain `/`. Only a tail can hold one that
    /// does, and a tail's other parameters must still stop at a separator.
    pub spans_separators: Vec<bool>,
}

/// One `/`-separated segment of a route's path.
#[derive(Debug, Clone)]
pub(crate) enum Segment {
    Static(String),
    Pattern(Pattern),
}

/// A compiled route and the path shape it is matched by.
#[derive(Debug)]
pub(crate) struct Compilation {
    pub segments: Vec<Segment>,
    /// Matched against everything from its segment to the end of the path, when
    /// the route ends in a parameter that spans separators.
    pub tail: Option<Pattern>,
    pub route: CompiledRoute,
}

enum Part<'a> {
    Literal(&'a str),
    Parameter(&'a ParameterSpec),
}

/// Compile a definition into the segments it matches.
///
/// A segment without parameters is matched as text. A segment with any is
/// matched by one expression built from its literal text and its parameters'
/// expressions, so a segment may hold several parameters and text around them,
/// as in `photo_<id><size>.<extension>`.
pub(crate) fn compile(definition: &RouteDefinition) -> Result<Compilation, RouteError> {
    let path = &definition.path;
    let mut segments: Vec<Vec<Part<'_>>> = vec![Vec::new()];
    let mut names: Vec<String> = Vec::new();
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let mut greedy_seen = false;
    let mut rest = path.as_str();
    let mut consumed = 0_usize;

    while let Some(open) = rest.find('<') {
        let literal = &rest[..open];
        reject_stray_close(path, literal, consumed)?;
        if greedy_seen && !literal.is_empty() {
            return Err(RouteError::GreedyParameterNotLast { path: path.clone() });
        }
        push_literal(&mut segments, literal);

        let after_open = &rest[open + 1..];
        let close = after_open
            .find('>')
            .ok_or_else(|| RouteError::UnclosedParameter {
                path: path.clone(),
                position: consumed + open,
            })?;

        let declaration = &after_open[..close];
        let name = declaration
            .rsplit_once(':')
            .map_or(declaration, |(_, name)| name);
        if name.is_empty() {
            return Err(RouteError::EmptyParameterName {
                path: path.clone(),
                name: declaration.to_owned(),
            });
        }
        if !seen.insert(name) {
            return Err(RouteError::DuplicateParameter {
                path: path.clone(),
                name: name.to_owned(),
            });
        }

        let spec = definition
            .parameters
            .iter()
            .find(|parameter| parameter.name == name)
            .ok_or_else(|| RouteError::UndescribedParameter {
                path: path.clone(),
                name: name.to_owned(),
            })?;
        if greedy_seen {
            return Err(RouteError::GreedyParameterNotLast { path: path.clone() });
        }
        // Checked on its own first, so a broken expression is reported against
        // the parameter it belongs to rather than the segment it ended up in.
        Regex::new(&format!("^(?:{})$", spec.pattern)).map_err(|error| {
            RouteError::UnusableExpression {
                path: path.clone(),
                name: spec.name.clone(),
                reason: error.to_string(),
            }
        })?;

        greedy_seen = spec.greedy;
        names.push(spec.name.clone());
        if let Some(segment) = segments.last_mut() {
            segment.push(Part::Parameter(spec));
        }

        consumed += open + 1 + close + 1;
        rest = &after_open[close + 1..];
    }

    reject_stray_close(path, rest, consumed)?;
    if greedy_seen && !rest.is_empty() {
        return Err(RouteError::GreedyParameterNotLast { path: path.clone() });
    }
    push_literal(&mut segments, rest);

    let mut compiled = Vec::with_capacity(segments.len());
    let mut tail = None;
    for parts in &segments {
        let has_parameters = parts.iter().any(|part| matches!(part, Part::Parameter(_)));
        if !has_parameters {
            let text: String = parts
                .iter()
                .map(|part| match part {
                    Part::Literal(text) => *text,
                    Part::Parameter(_) => "",
                })
                .collect();
            compiled.push(Segment::Static(text));
            continue;
        }

        let pattern = pattern(path, parts)?;
        if pattern.spans_separators.iter().any(|spans| *spans) {
            tail = Some(pattern);
        } else {
            compiled.push(Segment::Pattern(pattern));
        }
    }

    Ok(Compilation {
        segments: compiled,
        tail,
        route: CompiledRoute {
            id: definition.id,
            path: definition.path.clone(),
            methods: definition
                .methods
                .iter()
                .map(|method| method.to_ascii_uppercase())
                .collect(),
            parameters: names,
        },
    })
}

/// Literal text continues the current segment up to its first `/`, and every
/// `/` after that starts another.
fn push_literal<'a>(segments: &mut Vec<Vec<Part<'a>>>, literal: &'a str) {
    let mut pieces = literal.split('/');
    if let Some(first) = pieces.next()
        && let Some(segment) = segments.last_mut()
        && !first.is_empty()
    {
        segment.push(Part::Literal(first));
    }
    for piece in pieces {
        let mut segment = Vec::new();
        if !piece.is_empty() {
            segment.push(Part::Literal(piece));
        }
        segments.push(segment);
    }
}

/// Group names are positional within the expression and prefixed so they stay
/// apart from any names a parameter's own expression uses.
fn pattern(path: &str, parts: &[Part<'_>]) -> Result<Pattern, RouteError> {
    let mut source = String::from("^");
    let mut group_names = Vec::new();
    let mut spans_separators = Vec::new();
    let mut first_parameter = None;

    for part in parts {
        match part {
            Part::Literal(text) => source.push_str(&regex::escape(text)),
            Part::Parameter(spec) => {
                first_parameter.get_or_insert(spec.name.as_str());
                let group = format!("nitro_parameter_{}", group_names.len());
                source.push_str(&format!("(?P<{group}>{})", spec.pattern));
                group_names.push(group);
                spans_separators.push(spec.greedy);
            }
        }
    }
    source.push('$');

    let expression = Regex::new(&source).map_err(|error| RouteError::UnusableExpression {
        path: path.to_owned(),
        name: first_parameter.unwrap_or_default().to_owned(),
        reason: error.to_string(),
    })?;

    let groups = group_names
        .iter()
        .map(|group| {
            expression
                .capture_names()
                .position(|name| name == Some(group.as_str()))
                .ok_or_else(|| RouteError::UnusableExpression {
                    path: path.to_owned(),
                    name: first_parameter.unwrap_or_default().to_owned(),
                    reason: format!("capture group {group} went missing"),
                })
        })
        .collect::<Result<Vec<_>, _>>()?;

    Ok(Pattern {
        source,
        expression,
        groups,
        spans_separators,
    })
}

fn reject_stray_close(path: &str, literal: &str, offset: usize) -> Result<(), RouteError> {
    match literal.find('>') {
        Some(position) => Err(RouteError::UnopenedParameter {
            path: path.to_owned(),
            position: offset + position,
        }),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn definition(path: &str, parameters: Vec<ParameterSpec>) -> RouteDefinition {
        RouteDefinition::new(1, path, ["GET".to_owned()]).with_parameters(parameters)
    }

    fn sources(compilation: &Compilation) -> Vec<String> {
        compilation
            .segments
            .iter()
            .map(|segment| match segment {
                Segment::Static(text) => text.clone(),
                Segment::Pattern(pattern) => pattern.source.clone(),
            })
            .collect()
    }

    #[test]
    fn a_static_path_is_split_into_its_segments() {
        let compiled = compile(&definition("/about/", Vec::new())).unwrap();
        assert_eq!(sources(&compiled), vec!["", "about", ""]);
        assert!(compiled.route.parameters.is_empty());
        assert!(compiled.tail.is_none());
    }

    #[test]
    fn parameters_are_recorded_in_path_order() {
        let compiled = compile(&definition(
            "/users/<int:identifier>/posts/<slug:title>",
            vec![
                ParameterSpec::new("identifier", "[0-9]+"),
                ParameterSpec::new("title", "[-a-zA-Z0-9_]+"),
            ],
        ))
        .unwrap();

        assert_eq!(compiled.route.parameters, vec!["identifier", "title"]);
        assert_eq!(
            sources(&compiled),
            vec![
                "",
                "users",
                "^(?P<nitro_parameter_0>[0-9]+)$",
                "posts",
                "^(?P<nitro_parameter_0>[-a-zA-Z0-9_]+)$"
            ]
        );
    }

    #[test]
    fn a_segment_can_hold_text_and_several_parameters() {
        let compiled = compile(&definition(
            "/photos/photo_<id><size>.<extension>",
            vec![
                ParameterSpec::new("id", "[0-9]+"),
                ParameterSpec::new("size", "(?:_[0-9]+)?"),
                ParameterSpec::new("extension", "jpg|png"),
            ],
        ))
        .unwrap();

        let Some(Segment::Pattern(pattern)) = compiled.segments.last() else {
            panic!("expected the last segment to be a pattern");
        };
        assert_eq!(pattern.groups.len(), 3);
        assert!(pattern.expression.is_match("photo_12_64.png"));
        assert!(!pattern.expression.is_match("photo_12_64.gif"));
    }

    #[test]
    fn segments_with_the_same_expression_have_the_same_source() {
        let first = compile(&definition(
            "/things/<int:identifier>",
            vec![ParameterSpec::new("identifier", "[0-9]+")],
        ))
        .unwrap();
        let second = compile(&definition(
            "/things/<int:number>/more",
            vec![ParameterSpec::new("number", "[0-9]+")],
        ))
        .unwrap();

        assert_eq!(sources(&first)[2], sources(&second)[2]);
    }

    #[test]
    fn a_greedy_parameter_becomes_the_tail() {
        let compiled = compile(&definition(
            "/files/<path:rest>",
            vec![ParameterSpec::new("rest", ".+").greedy()],
        ))
        .unwrap();

        assert_eq!(sources(&compiled), vec!["", "files"]);
        let tail = compiled.tail.expect("a tail");
        assert_eq!(tail.spans_separators, vec![true]);
        assert!(tail.expression.is_match("deep/nested/file.txt"));
    }

    #[test]
    fn a_greedy_parameter_must_end_the_path() {
        let error = compile(&definition(
            "/files/<path:rest>/info",
            vec![ParameterSpec::new("rest", ".+").greedy()],
        ))
        .unwrap_err();
        assert!(matches!(error, RouteError::GreedyParameterNotLast { .. }));

        let error = compile(&definition(
            "/files/<path:rest><name>",
            vec![
                ParameterSpec::new("rest", ".+").greedy(),
                ParameterSpec::new("name", "[a-z]+"),
            ],
        ))
        .unwrap_err();
        assert!(matches!(error, RouteError::GreedyParameterNotLast { .. }));
    }

    #[test]
    fn an_expression_containing_a_colon_keeps_its_name() {
        let compiled = compile(&definition(
            "/assets/<regex(\"[a-z]{2}:[0-9]\"):tag>",
            vec![ParameterSpec::new("tag", "[a-z]{2}:[0-9]")],
        ))
        .unwrap();
        assert_eq!(compiled.route.parameters, vec!["tag"]);
    }

    #[test]
    fn an_expression_containing_a_slash_stays_in_its_segment() {
        let compiled = compile(&definition(
            "/assets/<regex(\"a/b\"):tag>.json",
            vec![ParameterSpec::new("tag", "[a-z]{2}")],
        ))
        .unwrap();
        assert_eq!(compiled.segments.len(), 3);
    }

    #[test]
    fn literal_text_is_escaped_in_an_expression() {
        let compiled = compile(&definition(
            "/assets/<lang>.json",
            vec![ParameterSpec::new("lang", "[a-z]{2}")],
        ))
        .unwrap();
        let Some(Segment::Pattern(pattern)) = compiled.segments.last() else {
            panic!("expected the last segment to be a pattern");
        };
        assert!(pattern.expression.is_match("de.json"));
        assert!(!pattern.expression.is_match("de_json"));
    }

    #[test]
    fn an_unclosed_parameter_is_reported_with_its_position() {
        let error = compile(&definition("/users/<int:identifier", Vec::new())).unwrap_err();
        assert_eq!(
            error,
            RouteError::UnclosedParameter {
                path: "/users/<int:identifier".to_owned(),
                position: 7
            }
        );
    }

    #[test]
    fn a_stray_closing_bracket_is_reported() {
        let error = compile(&definition("/users/identifier>", Vec::new())).unwrap_err();
        assert!(matches!(error, RouteError::UnopenedParameter { .. }));
    }

    #[test]
    fn a_parameter_the_caller_did_not_describe_is_reported() {
        let error = compile(&definition("/users/<int:identifier>", Vec::new())).unwrap_err();
        assert_eq!(
            error,
            RouteError::UndescribedParameter {
                path: "/users/<int:identifier>".to_owned(),
                name: "identifier".to_owned()
            }
        );
    }

    #[test]
    fn a_repeated_parameter_name_is_reported() {
        let error = compile(&definition(
            "/<str:name>/<str:name>",
            vec![ParameterSpec::new("name", "[^/]+")],
        ))
        .unwrap_err();
        assert!(matches!(error, RouteError::DuplicateParameter { .. }));
    }

    #[test]
    fn an_empty_parameter_name_is_reported() {
        let error = compile(&definition("/users/<int:>", Vec::new())).unwrap_err();
        assert!(matches!(error, RouteError::EmptyParameterName { .. }));
    }

    #[test]
    fn a_broken_expression_is_reported_against_its_parameter() {
        let error = compile(&definition(
            "/users/<first><bad:identifier>",
            vec![
                ParameterSpec::new("first", "[a-z]+"),
                ParameterSpec::new("identifier", "[0-9"),
            ],
        ))
        .unwrap_err();
        assert!(matches!(
            error,
            RouteError::UnusableExpression { ref name, .. } if name == "identifier"
        ));
    }

    #[test]
    fn expressions_must_match_the_whole_segment() {
        let compiled = compile(&definition(
            "/users/<int:identifier>",
            vec![ParameterSpec::new("identifier", "[0-9]+")],
        ))
        .unwrap();
        let Some(Segment::Pattern(pattern)) = compiled.segments.last() else {
            panic!("expected the last segment to be a pattern");
        };

        assert!(pattern.expression.is_match("42"));
        assert!(!pattern.expression.is_match("42abc"));
        assert!(!pattern.expression.is_match("abc42"));
    }

    #[test]
    fn alternatives_in_an_expression_stay_inside_their_parameter() {
        let compiled = compile(&definition(
            "/files/file.<extension>",
            vec![ParameterSpec::new("extension", "jpg|png")],
        ))
        .unwrap();
        let Some(Segment::Pattern(pattern)) = compiled.segments.last() else {
            panic!("expected the last segment to be a pattern");
        };

        assert!(pattern.expression.is_match("file.png"));
        assert!(!pattern.expression.is_match("png"));
    }

    #[test]
    fn head_is_answered_by_a_get_route() {
        let compiled = compile(&definition("/", Vec::new())).unwrap();
        assert!(compiled.route.accepts("GET"));
        assert!(compiled.route.accepts("HEAD"));
        assert!(!compiled.route.accepts("POST"));
    }

    #[test]
    fn methods_are_compared_in_upper_case() {
        let definition = RouteDefinition::new(1, "/", ["post".to_owned()]);
        let compiled = compile(&definition).unwrap();
        assert!(compiled.route.accepts("POST"));
    }
}
