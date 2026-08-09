from typing import Literal

# Type alias for version tuple: (major, minor, patch, release_level, serial)
VersionTuple = tuple[int, int, int, Literal["alpha", "beta", "rc", "final"], int]


def get_version(version: VersionTuple | None = None) -> str:
    """Return a PEP 440-compliant version number from VERSION."""
    version = get_complete_version(version)

    # Now build the two parts of the version number:
    # main = X.Y[.Z]
    # sub = .devN - for pre-alpha releases
    #     | {a|b|rc}N - for alpha, beta, and rc releases

    version = get_complete_version(version)
    parts = 2 if version[2] == 0 else 3
    main = ".".join(str(x) for x in version[:parts])

    sub = ""
    if version[3] != "final":
        mapping = {"alpha": "a", "beta": "b", "rc": "rc"}
        sub = mapping[version[3]] + str(version[4])

    return main + sub


def get_complete_version(version: VersionTuple | None = None) -> VersionTuple:
    """
    Return a tuple of the django version. If version argument is non-empty,
    check for correctness of the tuple provided.
    """
    if version is None:
        from nitro import VERSION as version
    else:
        assert len(version) == 5
        assert version[3] in ("alpha", "beta", "rc", "final")

    return version
