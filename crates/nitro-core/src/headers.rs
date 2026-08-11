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

//! Multi-value aware HTTP header map.
//!
//! HTTP allows a header name to appear more than once, so this wrapper keeps
//! every entry and exposes both a single-value and an all-values accessor. The
//! binding crate puts a dict-like surface on top of it; the semantics of that
//! surface are decided here so they can be tested without an interpreter.

use std::str::FromStr;

use http::header::{HeaderMap, HeaderName, HeaderValue};

#[derive(Debug, thiserror::Error)]
pub enum HeaderError {
    #[error("invalid header name: {0}")]
    Name(String),
    #[error("invalid header value for '{name}'")]
    Value { name: String },
}

#[derive(Debug, Clone, Default)]
pub struct Headers(HeaderMap);

impl Headers {
    pub fn new() -> Self {
        Self(HeaderMap::new())
    }

    pub fn as_map(&self) -> &HeaderMap {
        &self.0
    }

    pub fn as_map_mut(&mut self) -> &mut HeaderMap {
        &mut self.0
    }

    pub fn into_map(self) -> HeaderMap {
        self.0
    }

    /// The first value recorded for `name`, or `None` when the name is absent
    /// or its value is not valid UTF-8.
    pub fn get(&self, name: &str) -> Option<&str> {
        let name = HeaderName::from_str(name).ok()?;
        self.0.get(&name)?.to_str().ok()
    }

    /// Every value recorded for `name`, in the order received. Values that are
    /// not valid UTF-8 are skipped rather than replaced with a placeholder, so
    /// a caller never has to guess whether a string is real.
    pub fn get_all(&self, name: &str) -> Vec<&str> {
        let Ok(name) = HeaderName::from_str(name) else {
            return Vec::new();
        };
        self.0
            .get_all(&name)
            .iter()
            .filter_map(|value| value.to_str().ok())
            .collect()
    }

    pub fn contains(&self, name: &str) -> bool {
        HeaderName::from_str(name).is_ok_and(|name| self.0.contains_key(&name))
    }

    /// Every entry as a name/value pair, repeating the name once per value.
    pub fn items(&self) -> Vec<(&str, &str)> {
        self.0
            .iter()
            .filter_map(|(name, value)| Some((name.as_str(), value.to_str().ok()?)))
            .collect()
    }

    /// Every value, repeating nothing — one element per entry, matching [`items`].
    ///
    /// [`items`]: Self::items
    pub fn values(&self) -> Vec<&str> {
        self.0
            .values()
            .filter_map(|value| value.to_str().ok())
            .collect()
    }

    /// Each distinct header name exactly once.
    pub fn keys(&self) -> Vec<&str> {
        self.0.keys().map(HeaderName::as_str).collect()
    }

    /// The number of distinct header names.
    ///
    /// This intentionally disagrees with [`items`] and [`values`], which
    /// enumerate every entry: a map with `set-cookie` twice reports a length of
    /// one but yields two items. Counting names is what makes the type behave
    /// like the mapping it is presented as, and iteration over a mapping yields
    /// its keys. Please do not "fix" this to count entries — callers rely on
    /// `len` agreeing with `keys`.
    ///
    /// [`items`]: Self::items
    /// [`values`]: Self::values
    pub fn len(&self) -> usize {
        self.0.keys_len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// Replace any existing values for `name` with `value`.
    pub fn insert(&mut self, name: &str, value: &str) -> Result<(), HeaderError> {
        let (name, value) = parse_pair(name, value)?;
        self.0.insert(name, value);
        Ok(())
    }

    /// Add `value` while keeping any values already recorded for `name`.
    pub fn append(&mut self, name: &str, value: &str) -> Result<(), HeaderError> {
        let (name, value) = parse_pair(name, value)?;
        self.0.append(name, value);
        Ok(())
    }

    /// Remove every value for `name`, reporting whether anything was removed.
    pub fn remove(&mut self, name: &str) -> bool {
        HeaderName::from_str(name).is_ok_and(|name| self.0.remove(&name).is_some())
    }
}

impl From<HeaderMap> for Headers {
    fn from(map: HeaderMap) -> Self {
        Self(map)
    }
}

impl From<Headers> for HeaderMap {
    fn from(headers: Headers) -> Self {
        headers.0
    }
}

fn parse_pair(name: &str, value: &str) -> Result<(HeaderName, HeaderValue), HeaderError> {
    let parsed_name = HeaderName::from_str(name).map_err(|_| HeaderError::Name(name.to_owned()))?;
    let parsed_value = HeaderValue::from_str(value).map_err(|_| HeaderError::Value {
        name: name.to_owned(),
    })?;
    Ok((parsed_name, parsed_value))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Headers {
        let mut headers = Headers::new();
        headers.append("set-cookie", "a=1").unwrap();
        headers.append("set-cookie", "b=2").unwrap();
        headers.insert("content-type", "text/plain").unwrap();
        headers
    }

    #[test]
    fn get_returns_the_first_value() {
        assert_eq!(sample().get("set-cookie"), Some("a=1"));
    }

    #[test]
    fn get_all_returns_every_value_in_order() {
        assert_eq!(sample().get_all("set-cookie"), vec!["a=1", "b=2"]);
    }

    #[test]
    fn lookup_is_case_insensitive() {
        let headers = sample();
        assert_eq!(headers.get("Content-Type"), Some("text/plain"));
        assert!(headers.contains("CONTENT-TYPE"));
    }

    #[test]
    fn len_counts_names_while_items_counts_entries() {
        let headers = sample();
        assert_eq!(headers.len(), 2);
        assert_eq!(headers.keys().len(), 2);
        assert_eq!(headers.items().len(), 3);
        assert_eq!(headers.values().len(), 3);
    }

    #[test]
    fn insert_replaces_every_existing_value() {
        let mut headers = sample();
        headers.insert("set-cookie", "c=3").unwrap();
        assert_eq!(headers.get_all("set-cookie"), vec!["c=3"]);
    }

    #[test]
    fn remove_reports_whether_anything_was_present() {
        let mut headers = sample();
        assert!(headers.remove("set-cookie"));
        assert!(!headers.remove("set-cookie"));
        assert_eq!(headers.get_all("set-cookie"), Vec::<&str>::new());
    }

    #[test]
    fn missing_names_read_as_empty_rather_than_failing() {
        let headers = sample();
        assert_eq!(headers.get("x-absent"), None);
        assert_eq!(headers.get_all("x-absent"), Vec::<&str>::new());
        assert!(!headers.contains("x-absent"));
    }

    #[test]
    fn malformed_names_are_rejected_on_write_and_inert_on_read() {
        let mut headers = Headers::new();
        assert!(matches!(
            headers.insert("bad header", "value"),
            Err(HeaderError::Name(_))
        ));
        assert!(matches!(
            headers.insert("x-newline", "one\ntwo"),
            Err(HeaderError::Value { .. })
        ));
        assert_eq!(headers.get("bad header"), None);
        assert!(!headers.remove("bad header"));
    }

    #[test]
    fn values_that_are_not_utf8_are_skipped() {
        let mut map = HeaderMap::new();
        map.append(
            HeaderName::from_static("x-binary"),
            HeaderValue::from_bytes(&[0xff, 0xfe]).unwrap(),
        );
        map.append(
            HeaderName::from_static("x-binary"),
            "readable".parse().unwrap(),
        );
        let headers = Headers::from(map);

        assert_eq!(headers.get("x-binary"), None);
        assert_eq!(headers.get_all("x-binary"), vec!["readable"]);
        assert_eq!(headers.len(), 1);
    }
}
