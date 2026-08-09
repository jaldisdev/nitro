from typing import Any, Pattern

import regex as re


class ValidationError(Exception):
    """Raised when parameter validation fails."""

    def __init__(self, param_name: str, message: str):
        self.param_name = param_name
        self.message = message
        super().__init__(f"{param_name}: {message}")


class ParamBase:
    """
    Base class for all parameter types.

    Handles validation and extraction logic.
    """

    def __init__(
        self,
        default: Any = ...,
        *,
        alias: str | None = None,
        title: str | None = None,
        description: str | None = None,
        gt: float | None = None,
        ge: float | None = None,
        lt: float | None = None,
        le: float | None = None,
        min_length: int | None = None,
        max_length: int | None = None,
        regex: str | Pattern | None = None,
        example: Any = None,
    ):
        """
        Initialize parameter with validation constraints.

        Args:
            default: Default value. Use ... (Ellipsis) for required parameters
            alias: Alternative name for the parameter
            title: Human-readable title
            description: Parameter description
            gt: Greater than (exclusive)
            ge: Greater than or equal (inclusive)
            lt: Less than (exclusive)
            le: Less than or equal (inclusive)
            min_length: Minimum length for strings/lists
            max_length: Maximum length for strings/lists
            regex: Regular expression pattern for string validation
            example: Example value (for documentation)
        """
        self.default = default
        self.alias = alias
        self.title = title
        self.description = description
        self.gt = gt
        self.ge = ge
        self.lt = lt
        self.le = le
        self.min_length = min_length
        self.max_length = max_length
        self.regex = (
            regex
            if isinstance(regex, Pattern)
            else (re.compile(regex) if regex else None)
        )
        self.example = example
        self._param_name: str | None = None

    @property
    def required(self) -> bool:
        """Check if parameter is required."""
        return self.default is ...

    def validate(self, value: Any, param_name: str) -> Any:
        """
        Validate parameter value against constraints.

        Args:
            value: The value to validate
            param_name: Name of the parameter (for error messages)

        Returns:
            The validated value

        Raises:
            ValidationError: If validation fails
        """
        if value is None:
            if self.required:
                raise ValidationError(param_name, "field required")
            return self.default

        # Numeric validations
        if isinstance(value, (int, float)):
            if self.gt is not None and value <= self.gt:
                raise ValidationError(param_name, f"must be greater than {self.gt}")
            if self.ge is not None and value < self.ge:
                raise ValidationError(
                    param_name, f"must be greater than or equal to {self.ge}"
                )
            if self.lt is not None and value >= self.lt:
                raise ValidationError(param_name, f"must be less than {self.lt}")
            if self.le is not None and value > self.le:
                raise ValidationError(
                    param_name, f"must be less than or equal to {self.le}"
                )

        # String/list length validations
        if isinstance(value, (str, list, bytes)):
            length = len(value)
            if self.min_length is not None and length < self.min_length:
                raise ValidationError(
                    param_name, f"must be at least {self.min_length} characters"
                )
            if self.max_length is not None and length > self.max_length:
                raise ValidationError(
                    param_name, f"must be at most {self.max_length} characters"
                )

        # Regex validation for strings
        if isinstance(value, str) and self.regex is not None:
            if not self.regex.match(value):
                raise ValidationError(
                    param_name, f"must match pattern {self.regex.pattern}"
                )

        return value

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(default={self.default!r})"


class Query(ParamBase):
    """
    Query parameter extraction and validation.

    Example:
        async def handler(
            page: int = Query(1, ge=1),
            limit: int = Query(10, le=100),
            search: str = Query(None, min_length=3)
        ):
            ...
    """

    async def extract(self, request, param_name: str, param_type: type) -> Any:
        """Extract query parameter from request."""
        # Get the parameter name (use alias if provided)
        key = self.alias or param_name

        # Get value from query string
        value = request.query_params.get(key)

        if value is None:
            if self.required:
                raise ValidationError(param_name, "query parameter required")
            return self.default if self.default is not ... else None

        # Convert to target type
        try:
            if param_type is bool:
                # Handle boolean query params (true/false, 1/0, yes/no)
                value = value.lower() in ("true", "1", "yes", "on")
            elif param_type is int:
                value = int(value)
            elif param_type is float:
                value = float(value)
            elif param_type is list:
                # Handle multiple values for same param
                value = request.query_params.getlist(key)
            # str stays as-is
        except (ValueError, TypeError) as e:
            raise ValidationError(param_name, f"invalid type: {e}")

        # Validate
        return self.validate(value, param_name)


class Path(ParamBase):
    """
    Path parameter validation.

    Note: Path parameters are already extracted by the router,
    this just provides validation.

    Example:
        @app.http_route('/users/<int:user_id>')
        async def get_user(
            request: Request,
            user_id: int = Path(..., gt=0)
        ):
            ...
    """

    async def extract(self, request, param_name: str, param_type: type) -> Any:
        """Extract and validate path parameter."""
        # Path params are already in request.path_params
        key = self.alias or param_name
        value = request.path_params.get(key)

        if value is None:
            if self.required:
                raise ValidationError(param_name, "path parameter required")
            return self.default if self.default is not ... else None

        # Validate (type conversion already done by router)
        return self.validate(value, param_name)


class Header(ParamBase):
    """
    HTTP header extraction and validation.

    Example:
        async def handler(
            user_agent: str = Header(None),
            x_request_id: str = Header(...),  # Required
            content_type: str = Header('application/json', alias='content-type')
        ):
            ...
    """

    async def extract(self, request, param_name: str, param_type: type) -> Any:
        """Extract header from request."""
        # Convert python naming to HTTP header naming (user_agent -> user-agent)
        key = self.alias or param_name.replace("_", "-").lower()

        value = request.headers.get(key)

        if value is None:
            if self.required:
                raise ValidationError(param_name, f'header "{key}" required')
            return self.default if self.default is not ... else None

        # Validate
        return self.validate(value, param_name)


class Cookie(ParamBase):
    """
    Cookie extraction and validation.

    Example:
        async def handler(
            session_id: str = Cookie(None),
            user_token: str = Cookie(..., min_length=32)  # Required
        ):
            ...
    """

    async def extract(self, request, param_name: str, param_type: type) -> Any:
        """Extract cookie from request."""
        key = self.alias or param_name

        value = request.cookies.get(key)

        if value is None:
            if self.required:
                raise ValidationError(param_name, f'cookie "{key}" required')
            return self.default if self.default is not ... else None

        # Validate
        return self.validate(value, param_name)


class Body(ParamBase):
    r"""
    Request body field extraction and validation.

    For JSON request bodies. Can extract individual fields or entire body.

    Example:
        # Extract individual fields
        async def create_user(
            request: Request,
            name: str = Body(..., min_length=1),
            age: int = Body(..., ge=0, le=150),
            email: str = Body(..., regex=r'^[\w\.-]+@[\w\.-]+\.\w+$')
        ):
            ...

        # Or use with embed=True for nested body
        async def handler(
            request: Request,
            item: dict = Body(...)
        ):
            ...
    """

    def __init__(
        self,
        default: Any = ...,
        *,
        embed: bool = False,
        media_type: str = "application/json",
        **kwargs,
    ):
        """
        Initialize body parameter.

        Args:
            default: Default value
            embed: If True, expect field to be nested under param name
            media_type: Expected media type
            **kwargs: Validation constraints
        """
        super().__init__(default, **kwargs)
        self.embed = embed
        self.media_type = media_type

    async def extract(self, request, param_name: str, param_type: type) -> Any:
        """Extract field from request body."""
        if self.media_type != "application/json":
            raise ValidationError(
                param_name, f"unsupported media type: {self.media_type}"
            )

        # The request parses its body once and remembers the result, so several
        # parameters drawn from one body cost one parse between them.
        body = await request.json()

        if self.embed:
            # Expect {param_name: value}
            value = body.get(param_name)
        else:
            # Extract specific field
            key = self.alias or param_name
            value = body.get(key) if isinstance(body, dict) else None

        if value is None:
            if self.required:
                raise ValidationError(param_name, "body field required")
            return self.default if self.default is not ... else None

        # Validate
        return self.validate(value, param_name)


class File(ParamBase):
    """
    File upload extraction and validation.

    Example:
        async def upload(
            request: Request,
            file: bytes = File(...),
            filename: str = Query(...)
        ):
            # file contains the raw bytes
            ...

        # With size validation
        async def upload_avatar(
            request: Request,
            avatar: bytes = File(..., max_length=1024*1024)  # Max 1MB
        ):
            ...
    """

    def __init__(self, default: Any = ..., *, media_type: str | None = None, **kwargs):
        """
        Initialize file parameter.

        Args:
            default: Default value
            media_type: Expected media type (e.g., 'image/jpeg')
            **kwargs: Validation constraints (max_length for file size)
        """
        super().__init__(default, **kwargs)
        self.media_type = media_type

    async def extract(self, request, param_name: str, param_type: type) -> Any:
        """Extract file from request body."""
        # Get raw body
        body = await request.body()

        if not body:
            if self.required:
                raise ValidationError(param_name, "file required")
            return self.default if self.default is not ... else None

        # Validate media type if specified
        if self.media_type:
            content_type = request.headers.get("content-type", "")
            if not content_type.startswith(self.media_type):
                raise ValidationError(
                    param_name,
                    f"expected media type {self.media_type}, got {content_type}",
                )

        # Validate
        return self.validate(body, param_name)


# Convenience exports
__all__ = [
    "Query",
    "Path",
    "Header",
    "Cookie",
    "Body",
    "File",
    "ValidationError",
]
