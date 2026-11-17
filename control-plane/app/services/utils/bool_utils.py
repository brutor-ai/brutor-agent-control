# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Boolean parsing utilities for handling configuration values from various sources.
"""

from typing import Union


def parse_bool(value: Union[bool, str, None], default: bool = False) -> bool:
    """
    Parse boolean from various sources (env vars, JSON, etc.)

    Handles:
    - Actual booleans (True/False)
    - Strings: 'true', 'false', '1', '0', 'yes', 'no' (case insensitive)
    - None (returns default)

    Args:
        value: The value to parse
        default: Default value if value is None

    Returns:
        Boolean result

    Examples:
        >>> parse_bool("true")
        True
        >>> parse_bool("false")
        False
        >>> parse_bool("1")
        True
        >>> parse_bool("0")
        False
        >>> parse_bool("yes")
        True
        >>> parse_bool("no")
        False
        >>> parse_bool("TRUE")
        True
        >>> parse_bool(True)
        True
        >>> parse_bool(False)
        False
        >>> parse_bool(None)
        False
        >>> parse_bool(None, default=True)
        True
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ('true', '1', 'yes')