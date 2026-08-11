//! Which host names this server answers for.
//!
//! A client chooses the `Host` header, so a server that answers whatever it is
//! given will happily put an attacker's host name into anything built from it —
//! a password reset link, a cached response, an absolute redirect. Checking the
//! name against a list the deployment wrote down is what stops that.
//!
//! The check happens here rather than in the application because it needs to
//! happen before anything else does: a refused request never reaches the
//! interpreter, never resolves a dependency and never appears in a route.
//!
//! An empty list allows everything. That is the shape of "not configured yet",
//! which is right for the first `nitro app:app` of a new project; `nitro check`
//! refuses to pass a deployment that left it that way outside debug.

/// One entry of the allow list.
#[derive(Debug, Clone, PartialEq, Eq)]
enum HostPattern {
    /// `example.test` — this name and nothing else.
    Exact(String),
    /// `.example.test` — the domain and every subdomain of it. The stored text
    /// keeps its leading dot, so `evilexample.test` cannot match.
    Domain(String),
}

impl HostPattern {
    fn matches(&self, host: &str) -> bool {
        match self {
            Self::Exact(name) => name == host,
            // `.example.test` covers `example.test` itself as well as anything
            // under it, which is what a leading dot is usually taken to mean.
            Self::Domain(suffix) => host.ends_with(suffix) || host == &suffix[1..],
        }
    }
}

/// The host names a server answers for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AllowedHosts {
    patterns: Vec<HostPattern>,
    /// Set by an empty list or by `*`, both of which mean "do not check".
    unrestricted: bool,
}

impl Default for AllowedHosts {
    /// Nothing configured, which answers for every name.
    ///
    /// Written out rather than derived: a derived `bool` would be `false`, and
    /// an unconfigured server would refuse every request it was ever sent.
    fn default() -> Self {
        Self::new(Vec::<String>::new())
    }
}

impl AllowedHosts {
    /// Build the list from configured entries.
    ///
    /// Entries are lowered here so that every later comparison is a plain one:
    /// host names are case-insensitive, and doing the folding once per server
    /// beats doing it once per request.
    pub fn new<I, S>(entries: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut patterns = Vec::new();
        let mut unrestricted = false;

        for entry in entries {
            let entry = entry.as_ref().trim().to_ascii_lowercase();
            if entry.is_empty() {
                continue;
            }
            if entry == "*" {
                unrestricted = true;
                continue;
            }
            if let Some(stripped) = entry.strip_prefix('.') {
                patterns.push(HostPattern::Domain(format!(".{stripped}")));
            } else {
                patterns.push(HostPattern::Exact(entry));
            }
        }

        // Nothing configured is not the same as nothing allowed: it is a
        // project that has not reached deployment yet.
        let unrestricted = unrestricted || patterns.is_empty();
        Self {
            patterns,
            unrestricted,
        }
    }

    /// Whether every host is answered, so callers can skip the check entirely.
    pub fn is_unrestricted(&self) -> bool {
        self.unrestricted
    }

    /// Whether `authority` is one this server answers for.
    ///
    /// `authority` is the request target's authority on HTTP/2 and HTTP/3 and
    /// the `Host` header on HTTP/1.1. A request that carries neither is refused
    /// once the list is configured: there is nothing to check it against.
    pub fn permits(&self, authority: Option<&str>) -> bool {
        if self.unrestricted {
            return true;
        }
        let Some(authority) = authority else {
            return false;
        };

        let host = normalise(authority);
        if host.is_empty() {
            return false;
        }
        self.patterns.iter().any(|pattern| pattern.matches(&host))
    }
}

/// The host part of an authority, lowered and without its port.
///
/// Userinfo is dropped, an IPv6 literal keeps its brackets off, and a trailing
/// root dot is removed so `example.test.` and `example.test` are one name.
fn normalise(authority: &str) -> String {
    let after_userinfo = authority
        .rsplit_once('@')
        .map_or(authority, |(_, rest)| rest);

    let host = if let Some(rest) = after_userinfo.strip_prefix('[') {
        // `[::1]:8000` — everything to the closing bracket is the address.
        match rest.split_once(']') {
            Some((address, _port)) => address,
            None => rest,
        }
    } else {
        after_userinfo
            .split_once(':')
            .map_or(after_userinfo, |(host, _port)| host)
    };

    host.trim_end_matches('.').to_ascii_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nothing_configured_answers_for_everything() {
        let hosts = AllowedHosts::new(Vec::<String>::new());
        assert!(hosts.is_unrestricted());
        assert!(hosts.permits(Some("anything.test")));
        assert!(hosts.permits(None));
    }

    #[test]
    fn the_default_answers_for_everything() {
        let hosts = AllowedHosts::default();
        assert!(
            hosts.is_unrestricted(),
            "an unconfigured server must serve, not refuse everything"
        );
        assert!(hosts.permits(Some("anything.test")));
    }

    #[test]
    fn a_star_answers_for_everything() {
        let hosts = AllowedHosts::new(["*"]);
        assert!(hosts.is_unrestricted());
        assert!(hosts.permits(Some("anything.test")));
    }

    #[test]
    fn an_exact_name_matches_only_itself() {
        let hosts = AllowedHosts::new(["example.test"]);
        assert!(hosts.permits(Some("example.test")));
        assert!(!hosts.permits(Some("other.test")));
        assert!(!hosts.permits(Some("sub.example.test")));
    }

    #[test]
    fn a_leading_dot_matches_the_domain_and_its_subdomains() {
        let hosts = AllowedHosts::new([".example.test"]);
        assert!(hosts.permits(Some("example.test")));
        assert!(hosts.permits(Some("sub.example.test")));
        assert!(hosts.permits(Some("deep.sub.example.test")));
    }

    #[test]
    fn a_leading_dot_does_not_match_a_name_merely_ending_the_same_way() {
        let hosts = AllowedHosts::new([".example.test"]);
        assert!(
            !hosts.permits(Some("evilexample.test")),
            "the dot is part of the comparison"
        );
    }

    #[test]
    fn the_port_is_not_part_of_the_name() {
        let hosts = AllowedHosts::new(["example.test"]);
        assert!(hosts.permits(Some("example.test:8000")));
    }

    #[test]
    fn matching_ignores_case() {
        let hosts = AllowedHosts::new(["Example.Test"]);
        assert!(hosts.permits(Some("EXAMPLE.test")));
    }

    #[test]
    fn a_trailing_root_dot_is_the_same_name() {
        let hosts = AllowedHosts::new(["example.test"]);
        assert!(hosts.permits(Some("example.test.")));
        assert!(hosts.permits(Some("example.test.:8000")));
    }

    #[test]
    fn an_ipv6_literal_is_read_without_its_brackets() {
        let hosts = AllowedHosts::new(["::1"]);
        assert!(hosts.permits(Some("[::1]:8000")));
        assert!(hosts.permits(Some("[::1]")));
    }

    #[test]
    fn userinfo_is_not_part_of_the_name() {
        let hosts = AllowedHosts::new(["example.test"]);
        assert!(
            !hosts.permits(Some("example.test@evil.test")),
            "the host is what follows the '@'"
        );
        assert!(hosts.permits(Some("user@example.test")));
    }

    #[test]
    fn a_request_without_a_host_is_refused_once_the_list_is_configured() {
        let hosts = AllowedHosts::new(["example.test"]);
        assert!(!hosts.permits(None));
        assert!(!hosts.permits(Some("")));
    }

    #[test]
    fn blank_entries_are_ignored_rather_than_matching_nothing() {
        let hosts = AllowedHosts::new(["", "  ", "example.test"]);
        assert!(hosts.permits(Some("example.test")));
        assert!(!hosts.permits(Some("other.test")));
    }

    #[test]
    fn a_list_of_only_blanks_is_the_same_as_no_list() {
        assert!(AllowedHosts::new(["", "   "]).is_unrestricted());
    }
}
