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

    pass


class TemplateSyntaxError(TemplateError):
    """
    Raised when template has a syntax error.
    """

    pass
