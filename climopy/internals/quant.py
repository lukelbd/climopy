#!/usr/bin/env python3
"""
Tools for working with pint quantities.
"""
# NOTE: isinstance(..., ureg.Unit) or isinstance(..., ureg.Quantity) returns False
# for instances derived from other registries. So always test against pint namespace
# class definitions and defer to incompatible registry errors down the line.
import functools
import re

import numpy as np
import pint
import pint.util as putil
import xarray as xr

from ..unit import _to_pint_string, ureg
from . import docstring

__all__ = ['while_quantified', 'while_dequantified']

# Regex to find terms surrounded by curly braces that can be filled with str.format()
REGEX_FORMAT = re.compile(r'\{[a-zA-Z_]\w*\}')  # valid identifiers

# Docstring snippets
_quant_docstring = """
A decorator that executes functions with %(descrip)s data values and enforces the
specified input and output units. Pint quantities passed to the function will
result in quantities returned by the function. Non-pint quantiites passed to
the function are assumed to be in the correct units and will result in
non-quantities returned by the function.

Parameters
----------
units_in : unit-spec or str or sequence
    The units for the positional input arguments. Can be a `pint.Unit`, a unit
    string specification like ``'cm'``, or a relational variable specification like
    ``'=x'`` if the argument is not associated with any particular `pint.Unit`,
    for example ``while_%(descrip)s(('=x', '=y'), '=y / x')`` might be used
    for a function whose output units are the units of the second argument
    divided by the units of the first argument). Keyword arguments can be included
    in the variable specification using curly brace `str.format` notation
    after providing the default value via keyword argument, for example
    ``while_%(descrip)s(('=x', '=y'), '=y / x^{{order}}', order=1)`` might be
    used for a function that takes the nth derivative using the keyword `order`.
    Vertical bars can be used to allow multiple incompatible units, for
    example ``while_%(descrip)s('J | K', 'J / s | K / s')`` converts energy
    or temperature input values into corresponding rate of change terms. This
    can be useful for designing functions that execute similar physical
    operations with different (but related) physical quantities.
units_out : unit-spec or str or sequence
    As with `units_in`, but for the return values.
convert : bool, default: True
    Whether to convert input argument and return value units to the specified
    units or merely assert compatibility with the specified units.
strict : bool, default: False
    Whether to forbid non-quantity input arguments. If ``False`` then these
    are assumed to be in the correct units.
**fmt_defaults
    Default values for the terms surrounded by curly braces in relational
    or string unit specifications.

Returns
-------
callable
    The function decorated by `~internals.quant.while_%(descrip)s`.

Example
-------
Here is a simple example for an nth derivative wrapper.

>>> from climopy import ureg, while_%(descrip)s
>>> @while_%(descrip)s(('=x', '=y'), '=y / x^{{order}}', order=1)
... def deriv(x, y, order=1):
...     return y / x ** order
>>> deriv(1 * ureg.m, 1 * ureg.s, order=2)
<Quantity(1.0, 'second / meter ** 2')>
"""
docstring.snippets['quant.quantified'] = _quant_docstring


def _group_args(args_in, args_out):
    """
    Parse specifications for input arguments and return values. Used for
    `while_quantified`, `while_dequantified`, and `register_derivation`.
    """
    # Enforce iterable input argument and return value specs
    # NOTE: Type checking for decorators happens here first.
    from ..cfvariable import CFVariable  # depends on internals so import here
    if isinstance(args_in, str) or not np.iterable(args_in):
        args_in = () if args_in is None else (args_in,)
    if is_scalar_out := isinstance(args_out, str) or not np.iterable(args_out):
        args_out = () if args_out is None else (args_out,)

    # Split string specs into groups of options (separated by |). Ensure same
    # number of non-scalar options for input arguments and return values.
    args = []
    sizes = set()
    for i, arg in enumerate((*args_in, *args_out)):
        if isinstance(arg, str):
            arg = [a.strip() for a in arg.split('|')]
        elif arg is None or isinstance(arg, (pint.Unit, CFVariable)):
            arg = [arg]
        else:
            raise TypeError(f'Input must be str, dict, Unit, or UnitsContainer. Instead got {arg!r}.')  # noqa: E501
        if len(arg) > 1:
            sizes.add(len(arg))
        if len(sizes) > 1:
            raise TypeError('Non-scalar name sequences must be equal length.')
        if sizes and len(arg) == 1 and i >= len(args_in):
            raise TypeError('Non-scalar input name sequences require non-scalar output')
        args.append(arg)

    # Split input argument and return value specs into option groups. For example get
    # [['a', 'c'], ['b', 'c']] and [['x'], ['y']] from _group_args(('a|b', 'c'), 'x|y').
    args = [arg * max(sizes, default=1) if len(arg) == 1 else arg for arg in args]
    args_in = [arg[:-len(args_out)] for arg in zip(*args)]
    args_out = [arg[-len(args_out):] for arg in zip(*args)]
    return args_in, args_out, is_scalar_out


def _units_container(arg, **fmt_kwargs):
    """
    Convert a unit type to a UnitsContainer after checking if it is a reference.
    """
    # NOTE: This parses units when applying quantify decoraters and when
    # standardizing return value units and dependent input argument units.
    is_ref = False
    if isinstance(arg, str):
        arg = arg.format(**fmt_kwargs)  # permit extra keyword arguments
        if '=' in arg:  # avoid reading numeric variable suffixes as exponents
            is_ref = True
        else:
            arg = _to_pint_string(arg)  # support conventions
    elif isinstance(arg, pint.Unit):
        pass
    elif arg is not None:  # should be impossible since _group_args checks type
        raise ValueError(f'Unrecognized pint unit argument {arg}.')
    if is_ref:
        container = putil.to_units_container(arg.split('=', 1)[1])
    else:
        container = putil.to_units_container(arg, ureg)  # None returns None
    return container, is_ref


def _units_object(arg):
    """
    Get the pint units associated with the object argument.
    """
    # NOTE: This parses units when comparing input argument units against the
    # declared units in order to select the correct standardization group.
    units = None
    if isinstance(arg, str):
        arg = ureg.parse_expression(arg)  # multiplies raw unit strings by '1'
    elif isinstance(arg, pint.Unit):
        units = arg
    elif isinstance(arg, pint.Quantity):
        units = arg.units
    elif isinstance(arg, xr.DataArray):
        if arg.climo._is_quantity or 'units' in arg.attrs:
            units = arg.climo.units
    return units


def _standardize_independent(arg, quantify=False):
    """
    Return a quantified version of the input argument using its own units. If it
    has no units then assign dimensionless units.
    """
    # Apply existing units
    if isinstance(arg, str):  # parse expressions e.g. '5cm'
        arg = ureg.parse_expression(arg)
    if isinstance(arg, xr.DataArray):
        if not (has_units := arg.climo._is_quantity):
            arg = arg.climo.quantify(units=arg.attrs.get('units', 'dimensionless'))
        units = arg.data.units
    else:
        if not (has_units := isinstance(arg, pint.Quantity)):
            arg = arg * ureg.dimensionless
        units = arg.units

    # Optionally dequantify result after converting
    if not quantify:
        if isinstance(arg, xr.DataArray):
            arg = arg.climo.dequantify()
        else:
            arg = arg.magnitude
    return arg, units, has_units


def _standardize_dependent(
    arg, unit=None, convert=True, strict=False, quantify=False, definitions=None,
    **fmt_kwargs
):
    """
    Return a quantified version of the input argument possibly applying the
    declared units or inferring them from the independent variable units.
    """
    # Parse input argument
    if unit is None:  # placeholder meaning 'do nothing'
        return arg, False
    if isinstance(arg, str):  # parse expressions e.g. '5cm'
        arg = ureg.parse_expression(arg)
    if isinstance(arg, xr.DataArray) and 'units' in arg.attrs:
        arg = arg.climo.quantify()

    # Parse input units
    # NOTE: Here definitions are required if input is refernece
    container, is_ref = _units_container(unit, **fmt_kwargs)
    if not is_ref:
        unit = ureg.Unit(container)
    else:
        unit = ureg.dimensionless
        definitions = definitions or {}
        for name, exponent in container.items():
            if name in definitions:
                unit *= definitions[name] ** exponent
            else:
                raise RuntimeError(f'Missing unit definition for variable {name!r}.')

    # Enforce argument units
    # NOTE: Important to record whether we started with units
    if isinstance(arg, pint.Quantity):
        has_units = True
        if convert:
            arg = arg.to(unit)
        else:
            arg + 0 * unit  # trigger compatibility check
    elif isinstance(arg, xr.DataArray) and arg.climo._is_quantity:
        has_units = True
        if convert:
            arg = arg.climo.to(unit)
        else:
            arg + 0 * unit  # trigger compatibility check
    elif not strict:
        has_units = False
        if isinstance(arg, xr.DataArray):
            arg = arg.climo.quantify(units=unit)
        else:
            arg = ureg.Quantity(arg, unit)
    else:
        raise ValueError('Pint quantities are required in strict mode.')

    # Optionally dequantify result after converting
    if not quantify:
        if isinstance(arg, xr.DataArray):
            arg = arg.climo.dequantify()
        else:
            arg = arg.magnitude
    return arg, has_units


def _while_converted(
    units_in=None,
    units_out=None,
    convert=True,
    strict=False,
    grouped=False,
    quantify=False,
    **fmt_defaults  # noqa: E501
):
    """
    Driver function for `while_quantified` and `while_dequantified`. See above
    for the full documentation.
    """
    # Group and categorize the input argument units
    # NOTE: Resulting units_in, units_out will be singleton lists unless | was used.
    categories = []
    containers = []  # check dependent against independent variables
    if grouped:  # used by register_derivation
        pass
    else:
        units_in, units_out, is_scalar_out = _group_args(units_in, units_out)
    for units in units_in:
        independent = {}
        dependent = set()
        constant = set()
        for idx, unit in enumerate(units):
            container, is_ref = _units_container(unit, **fmt_defaults)  # type checking
            if container is None:
                pass
            elif is_ref:
                if len(container) == 1:
                    (key, value), = container.items()
                    if value == 1 and key not in independent:
                        independent[key] = idx
                    else:
                        dependent.add(idx)
                else:
                    dependent.add(idx)  # definition is found elsewhere
            else:
                constant.add(idx)
            containers.append(container)
        for idx in dependent:
            container, unit = containers[idx], unit[idx]
            if not container.keys() <= independent.keys():
                raise ValueError(f'Not all variables referenced in {unit} are defined.')
        categories.append((independent, dependent, constant))

    # Declare decorator
    def _decorator(func):
        @functools.wraps(func)
        def _wrapper(*args, **kwargs):
            # Test input arguments. Bypass extra arguments
            args = list(args)
            n_result = len(args)
            n_expect = len(units_in[0])
            if n_expect > n_result:
                raise ValueError(f'Expected {n_expect} positional args, got {n_result}.')  # noqa: E501

            # Select group for parsing
            # NOTE: Behavior is subtle. Iterate over possible inputs and approve each
            # member of the grouping if either (1) it has no units, (2) the declared
            # unit is a reference, (3) there were no declared units, or (4) the units
            # are compatible with the declared units. If this fails we use the final
            # grouping by default and an error will be raised down the line.
            for grp, (independents, dependents, constants) in enumerate(categories):
                units_input = [_units_object(args[idx]) for idx in constants]
                units_expect = [_units_object(units_in[grp][idx]) for idx in constants]
                if all(
                    unit_input is None
                    or unit_expect is None
                    or unit_input.is_compatible_with(unit_expect)
                    for unit_input, unit_expect in zip(units_input, units_expect)
                ):
                    break

            # Quantify independent input arguments and record units
            args_new = args.copy()
            definitions = {}
            quantify_results = False
            for key, idx in independents.items():
                arg, unit, has_units = _standardize_independent(
                    args[idx],
                    quantify=quantify
                )
                args_new[idx] = arg
                definitions[key] = unit
                quantify_results = has_units or quantify_results

            # Quantify remaining arguments using recorded units
            fmt_kwargs = {key: val for key, val in kwargs.items() if key in fmt_defaults}  # noqa: E501
            for key, val in fmt_defaults.items():
                fmt_kwargs.setdefault(key, value)
            for idx in (*dependents, *constants):
                arg, has_units = _standardize_dependent(
                    args[idx],
                    units_in[grp][idx],
                    strict=strict,
                    convert=convert,
                    quantify=quantify,
                    definitions=definitions,
                    **fmt_kwargs
                )
                args_new[idx] = arg
                quantify_results = has_units or quantify_results

            # Call main function and standardize results. Bypass extra values
            results = func(*args_new, **kwargs)
            n_result = 1 if not isinstance(results, tuple) else len(results)
            n_expect = len(units_out[grp])
            if is_scalar_out and isinstance(results, tuple):
                raise ValueError('Got tuple of return values, expected one value.')
            if not is_scalar_out and n_expect > n_result:
                raise ValueError(f'Expected {n_expect} return values, got {n_result}.')
            results = [results] if is_scalar_out else list(results)
            results_new = results.copy()
            for idx in range(n_expect):
                res, _ = _standardize_dependent(
                    results[idx],
                    units_out[grp][idx],
                    convert=convert,
                    quantify=quantify_results,
                    definitions=definitions,
                    **fmt_kwargs
                )
                results_new[idx] = res

            # Return sanitized values
            if is_scalar_out:
                return results_new[0]
            else:
                return tuple(results_new)

        return _wrapper

    return _decorator


@docstring.inject_snippets(descrip='dequantified')
def while_quantified(*args, **kwargs):
    """
    %(quant.quantified)s
    """
    kwargs['quantify'] = True
    return _while_converted(*args, **kwargs)


@docstring.inject_snippets(descrip='dequantified')
def while_dequantified(*args, **kwargs):
    """
    %(quant.quantified)s
    """
    kwargs['quantify'] = False
    return _while_converted(*args, **kwargs)
