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


class TemplateDoesNotExist(Exception):
    """
    Raised when a template cannot be found.
    """

    def __init__(self, message: str, tried: list[str] | None = None):
        super().__init__(message)
        self.tried = tried or []


class TemplateError(Exception):
    """
    Base exception for template rendering errors.
    """


class TemplateSyntaxError(TemplateError):
    """
    Raised when template has a syntax error.
    """
