#
# This source file is part of the Nitro open source project.
#
# Copyright (c) 2026 Jaldis B.V.
#
# Licensed under the MIT OR Apache-2.0 license (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://opensource.org/licenses/MIT
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

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
    Return a tuple of the version. If version argument is non-empty,
    check for correctness of the tuple provided.
    """
    if version is None:
        from nitro import VERSION as version
    else:
        assert len(version) == 5
        assert version[3] in ("alpha", "beta", "rc", "final")

    return version
