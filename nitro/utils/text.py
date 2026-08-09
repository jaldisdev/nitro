def capitalize_first(s: str) -> str:
    """Capitalize the first letter of a string."""
    if not s:
        return s
    if not isinstance(s, str):
        s = str(s)
    return s[:1].upper() + s[1:]


def lower_first(s: str):
    """
    Returns input string with first letter lowercased
    """
    
    if not s:
        return s
    if not isinstance(s, str):
        s = str(s)
    return s[:1].lower() + s[1:]


def to_camel_case(s: str, capitalize: bool = False) -> str:
    """
    Converts input string into camel case variant.
    """
    
    ret_val = re.sub(r'_([a-zA-Z0-9])', lambda x: x.group(1).upper(), sstring)
    if capitalize:
        ret_val = ret_val[0].upper() + ret_val[1:]
    return ret_val


def to_snake_case(s: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', s, 0).lower()
