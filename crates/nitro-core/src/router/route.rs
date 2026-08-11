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

/// One parameter of a compiled route, in the order it appears in the path.
#[derive(Debug)]
pub struct CompiledParameter {
    pub name: String,
    pub expression: Regex,
}

/// A route ready to be matched against.
#[derive(Debug)]
pub struct CompiledRoute {
    pub id: u64,
    pub path: String,
    pub methods: BTreeSet<String>,
    pub parameters: Vec<CompiledParameter>,
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

/// A compiled route together with the structural template it matched under.
#[derive(Debug)]
pub(crate) struct Compilation {
    pub template: String,
    pub route: CompiledRoute,
}

/// Compile a definition into a template for the structural matcher plus the
/// expressions its parameters must satisfy.
///
/// Parameter names are replaced with positional placeholders in the template.
/// Two routes that differ only in what their parameters accept — say an
/// identifier that must be digits and a title that must be a slug — therefore
/// share one template and are told apart afterwards, by their expressions,
/// rather than colliding in the structural matcher.
pub(crate) fn compile(definition: &RouteDefinition) -> Result<Compilation, RouteError> {
    let path = &definition.path;
    let mut template = String::with_capacity(path.len());
    let mut parameters: Vec<CompiledParameter> = Vec::new();
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let mut rest = path.as_str();
    let mut consumed = 0_usize;

    while let Some(open) = rest.find('<') {
        let literal = &rest[..open];
        reject_stray_close(path, literal, consumed)?;
        push_literal(&mut template, literal);

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

        let placeholder = parameters.len();
        if spec.greedy {
            template.push_str(&format!("{{*p{placeholder}}}"));
        } else {
            template.push_str(&format!("{{p{placeholder}}}"));
        }

        parameters.push(CompiledParameter {
            name: spec.name.clone(),
            expression: anchored(&spec.pattern).map_err(|reason| {
                RouteError::UnusableExpression {
                    path: path.clone(),
                    name: spec.name.clone(),
                    reason,
                }
            })?,
        });

        consumed += open + 1 + close + 1;
        rest = &after_open[close + 1..];

        if spec.greedy && !rest.is_empty() {
            return Err(RouteError::GreedyParameterNotLast { path: path.clone() });
        }
    }

    reject_stray_close(path, rest, consumed)?;
    push_literal(&mut template, rest);

    Ok(Compilation {
        template,
        route: CompiledRoute {
            id: definition.id,
            path: definition.path.clone(),
            methods: definition
                .methods
                .iter()
                .map(|method| method.to_ascii_uppercase())
                .collect(),
            parameters,
        },
    })
}

/// Braces are how the structural matcher spells a parameter, so a literal one
/// in the path has to be doubled to survive.
fn push_literal(template: &mut String, literal: &str) {
    for character in literal.chars() {
        match character {
            '{' => template.push_str("{{"),
            '}' => template.push_str("}}"),
            other => template.push(other),
        }
    }
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

/// Require the expression to match the whole captured value, so a pattern for
/// digits cannot quietly accept `12abc`.
fn anchored(pattern: &str) -> Result<Regex, String> {
    Regex::new(&format!("^(?:{pattern})$")).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn definition(path: &str, parameters: Vec<ParameterSpec>) -> RouteDefinition {
        RouteDefinition::new(1, path, ["GET".to_owned()]).with_parameters(parameters)
    }

    #[test]
    fn a_static_path_compiles_to_itself() {
        let compiled = compile(&definition("/about/", Vec::new())).unwrap();
        assert_eq!(compiled.template, "/about/");
        assert!(compiled.route.parameters.is_empty());
    }

    #[test]
    fn parameters_become_positional_placeholders() {
        let compiled = compile(&definition(
            "/users/<int:identifier>/posts/<slug:title>",
            vec![
                ParameterSpec::new("identifier", "[0-9]+"),
                ParameterSpec::new("title", "[-a-zA-Z0-9_]+"),
            ],
        ))
        .unwrap();

        assert_eq!(compiled.template, "/users/{p0}/posts/{p1}");
        let names: Vec<&str> = compiled
            .route
            .parameters
            .iter()
            .map(|parameter| parameter.name.as_str())
            .collect();
        assert_eq!(names, vec!["identifier", "title"]);
    }

    #[test]
    fn two_routes_differing_only_in_what_they_accept_share_a_template() {
        let first = compile(&definition(
            "/things/<int:identifier>",
            vec![ParameterSpec::new("identifier", "[0-9]+")],
        ))
        .unwrap();
        let second = compile(&definition(
            "/things/<slug:name>",
            vec![ParameterSpec::new("name", "[-a-z]+")],
        ))
        .unwrap();

        assert_eq!(first.template, second.template);
    }

    #[test]
    fn a_parameter_without_a_converter_still_works() {
        let compiled = compile(&definition(
            "/users/<identifier>",
            vec![ParameterSpec::new("identifier", "[^/]+")],
        ))
        .unwrap();
        assert_eq!(compiled.template, "/users/{p0}");
    }

    #[test]
    fn a_greedy_parameter_becomes_a_catch_all() {
        let compiled = compile(&definition(
            "/files/<path:rest>",
            vec![ParameterSpec::new("rest", ".+").greedy()],
        ))
        .unwrap();
        assert_eq!(compiled.template, "/files/{*p0}");
    }

    #[test]
    fn a_greedy_parameter_must_end_the_path() {
        let error = compile(&definition(
            "/files/<path:rest>/info",
            vec![ParameterSpec::new("rest", ".+").greedy()],
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
        assert_eq!(compiled.route.parameters[0].name, "tag");
    }

    #[test]
    fn literal_braces_are_escaped() {
        let compiled = compile(&definition("/curly/{literal}/", Vec::new())).unwrap();
        assert_eq!(compiled.template, "/curly/{{literal}}/");
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
    fn a_broken_expression_is_reported() {
        let error = compile(&definition(
            "/users/<bad:identifier>",
            vec![ParameterSpec::new("identifier", "[0-9")],
        ))
        .unwrap_err();
        assert!(matches!(error, RouteError::UnusableExpression { .. }));
    }

    #[test]
    fn expressions_must_match_the_whole_value() {
        let compiled = compile(&definition(
            "/users/<int:identifier>",
            vec![ParameterSpec::new("identifier", "[0-9]+")],
        ))
        .unwrap();
        let expression = &compiled.route.parameters[0].expression;

        assert!(expression.is_match("42"));
        assert!(!expression.is_match("42abc"));
        assert!(!expression.is_match("abc42"));
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
