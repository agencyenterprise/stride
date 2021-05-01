
import os
import gc
import devito
import logging
import functools
import itertools
import numpy as np
import scipy.special

import mosaic
from mosaic.types import Struct


__all__ = ['OperatorDevito', 'GridDevito']


class FullDomain(devito.SubDomain):

    name = 'full_domain'

    def __init__(self, space_order, extra):
        super().__init__()

        self.space_order = space_order
        self.extra = extra

    def define(self, dimensions):
        return {dimension: dimension for dimension in dimensions}


class InteriorDomain(devito.SubDomain):

    name = 'interior_domain'

    def __init__(self, space_order, extra):
        super().__init__()

        self.space_order = space_order
        self.extra = extra

    def define(self, dimensions):
        return {dimension: ('middle', extra, extra)
                for dimension, extra in zip(dimensions, self.extra)}


class PMLSide(devito.SubDomain):

    def __init__(self, space_order, extra, dim, side):
        self.dim = dim
        self.side = side
        self.name = 'pml_side_' + side + str(dim)

        super().__init__()

        self.space_order = space_order
        self.extra = extra

    def define(self, dimensions):
        domain = {dimension: dimension for dimension in dimensions}
        domain[dimensions[self.dim]] = (self.side, self.extra[self.dim])

        return domain


class PMLCentre(devito.SubDomain):

    def __init__(self, space_order, extra, dim, side):
        self.dim = dim
        self.side = side
        self.name = 'pml_centre_' + side + str(dim)

        super().__init__()

        self.space_order = space_order
        self.extra = extra

    def define(self, dimensions):
        domain = {dimension: ('middle', extra, extra)
                  for dimension, extra in zip(dimensions, self.extra)}
        domain[dimensions[self.dim]] = (self.side, self.extra[self.dim])

        return domain


class PMLCorner(devito.SubDomain):

    def __init__(self, space_order, extra, *sides):
        self.sides = sides
        self.name = 'pml_corner_' + '_'.join(sides)

        super().__init__()

        self.space_order = space_order
        self.extra = extra

    def define(self, dimensions):
        domain = {dimension: (side, extra)
                  for dimension, side, extra in zip(dimensions, self.sides, self.extra)}

        return domain


class PMLPartial(devito.SubDomain):

    def __init__(self, space_order, extra, dim, side):
        self.dim = dim
        self.side = side
        self.name = 'pml_partial_' + side + str(dim)

        super().__init__()

        self.space_order = space_order
        self.extra = extra

    def define(self, dimensions):
        domain = {dimension: ('middle', extra, extra)
                  for dimension, extra in zip(dimensions, self.extra)}
        domain[dimensions[0]] = dimensions[0]
        domain[dimensions[self.dim]] = (self.side, self.extra[self.dim])

        return domain


def _cached(func):

    @functools.wraps(func)
    def cached_wrapper(self, *args, **kwargs):
        name = args[0]
        cached = kwargs.pop('cached', True)

        if cached is True:
            fun = self.vars.get(name, None)
            if fun is not None:
                return fun

        fun = func(self, *args, **kwargs)

        self.vars[name] = fun

        return fun

    return cached_wrapper


class GridDevito:
    """
    Instances of this class encapsulate the Devito grid, and interact with it by
    generating appropriate functions on demand.

    Instances will also keep a cache of created Devito functions under the ``vars``
    attribute, which can be accessed by name using dot notation.

    Parameters
    ----------
    space_order : int
        Default space order of the discretisation for functions of the grid.
    time_order : int
        Default time order of the discretisation for functions of the grid.
    grid : devito.Grid, optional
        Predefined Devito grid. A new one will be created unless specified.

    """

    def __init__(self, space_order, time_order, grid=None):
        self._problem = None

        self.vars = Struct()

        self.space_order = space_order
        self.time_order = time_order

        self.grid = grid

        self.full = None
        self.interior = None
        self.pml = None
        self.pml_centres = None
        self.pml_corners = None
        self.pml_partials = None
        self.pml_left = None
        self.pml_right = None

    # TODO The grid needs to be re-created if the space or time extent has changed
    def set_problem(self, problem):
        """
        Set up the problem or sub-problem that will be run on this grid.

        Parameters
        ----------
        problem : SubProblem or Problem
            Problem on which the physics will be executed

        Returns
        -------

        """
        self._problem = problem

        if self.grid is None:
            space = problem.space
            order = self.space_order
            extra = space.absorbing

            extended_extent = tuple(np.array(space.spacing) * (np.array(space.extended_shape) - 1))

            self.full = FullDomain(order, extra)
            self.interior = InteriorDomain(order, extra)
            self.pml_left = tuple()
            self.pml_right = tuple()
            self.pml_centres = tuple()
            self.pml_partials = tuple()

            for dim in range(space.dim):
                self.pml_left += (PMLSide(order, extra, dim, 'left'),)
                self.pml_right += (PMLSide(order, extra, dim, 'right'),)
                self.pml_centres += (PMLCentre(order, extra, dim, 'left'),
                                     PMLCentre(order, extra, dim, 'right'))
                self.pml_partials += (PMLPartial(order, extra, dim, 'left'),
                                      PMLPartial(order, extra, dim, 'right'))

            self.pml_corners = [PMLCorner(order, extra, *sides)
                                for sides in itertools.product(['left', 'right'],
                                                               repeat=space.dim)]
            self.pml_corners = tuple(self.pml_corners)

            self.pml = self.pml_partials

            self.grid = devito.Grid(extent=extended_extent,
                                    shape=space.extended_shape,
                                    origin=space.pml_origin,
                                    subdomains=(self.full, self.interior,) +
                                               self.pml + self.pml_left + self.pml_right +
                                               self.pml_centres + self.pml_corners,
                                    dtype=np.float32)

    @_cached
    def sparse_time_function(self, name, num=1, space_order=None, time_order=None,
                             coordinates=None, interpolation_type='linear', **kwargs):
        """
        Create a Devito SparseTimeFunction with parameters provided.

        Parameters
        ----------
        name : str
            Name of the function.
        num : int, optional
            Number of points in the function, defaults to 1.
        space_order : int, optional
            Space order of the discretisation, defaults to the grid space order.
        time_order : int, optional
            Time order of the discretisation, defaults to the grid time order.
        coordinates : ndarray, optional
            Spatial coordinates of the sparse points (num points, dimensions), only
            needed when interpolation is not linear.
        interpolation_type : str, optional
            Type of interpolation to perform (``linear`` or ``hicks``), defaults
            to ``linear``, computationally more efficient but less accurate.
        kwargs
            Additional arguments for the Devito constructor.

        Returns
        -------
        devito.SparseTimeFunction
            Generated function.

        """
        time = self._problem.time

        space_order = space_order or self.space_order
        time_order = time_order or self.time_order

        # Define variables
        p_dim = devito.Dimension(name='p_%s' % name)

        sparse_kwargs = dict(name=name,
                             grid=self.grid,
                             dimensions=(self.grid.time_dim, p_dim),
                             npoint=num,
                             nt=time.extended_num,
                             space_order=space_order,
                             time_order=time_order,
                             dtype=np.float32)
        sparse_kwargs.update(kwargs)

        if interpolation_type == 'linear':
            fun = devito.SparseTimeFunction(**sparse_kwargs)

        elif interpolation_type == 'hicks':
            r = sparse_kwargs.pop('r', 7)

            reference_gridpoints, coefficients = self.calculate_hicks(coordinates)

            fun = devito.PrecomputedSparseTimeFunction(r=r,
                                                       gridpoints=reference_gridpoints,
                                                       interpolation_coeffs=coefficients,
                                                       **sparse_kwargs)

        else:
            raise ValueError('Only "linear" and "hicks" interpolations are allowed.')

        return fun

    def calculate_hicks(self, coordinates):
        space = self._problem.space

        # Calculate the reference gridpoints and offsets
        grid_coordinates = (coordinates - np.array(space.pml_origin)) / np.array(space.spacing)
        reference_gridpoints = np.round(grid_coordinates).astype(np.int32)
        offsets = grid_coordinates - reference_gridpoints

        # Pre-calculate stuff
        kaiser_b = 4.14
        kaiser_half_width = 3
        kaiser_den = scipy.special.iv(0, kaiser_b)
        kaiser_extended_width = kaiser_half_width/0.99

        # Calculate coefficients
        r = 2*kaiser_half_width+1
        num = coordinates.shape[0]
        coefficients = np.zeros((num, space.dim, r))

        for grid_point in range(-kaiser_half_width, kaiser_half_width+1):
            index = kaiser_half_width + grid_point

            x = grid_point + offsets

            weights = (x / kaiser_extended_width)**2
            weights[weights > 1] = 1
            weights = scipy.special.iv(0, kaiser_b * np.sqrt(1 - weights)) / kaiser_den

            coefficients[:, :, index] = np.sinc(x) * weights

        return reference_gridpoints - kaiser_half_width, coefficients

    @_cached
    def function(self, name, space_order=None, **kwargs):
        """
        Create a Devito Function with parameters provided.

        Parameters
        ----------
        name : str
            Name of the function.
        space_order : int, optional
            Space order of the discretisation, defaults to the grid space order.
        kwargs
            Additional arguments for the Devito constructor.

        Returns
        -------
        devito.Function
            Generated function.

        """
        space_order = space_order or self.space_order

        fun = devito.Function(name=name,
                              grid=self.grid,
                              space_order=space_order,
                              dtype=np.float32,
                              **kwargs)

        return fun

    @_cached
    def time_function(self, name, space_order=None, time_order=None, **kwargs):
        """
        Create a Devito TimeFunction with parameters provided.

        Parameters
        ----------
        name : str
            Name of the function.
        space_order : int, optional
            Space order of the discretisation, defaults to the grid space order.
        time_order : int, optional
            Time order of the discretisation, defaults to the grid time order.
        kwargs
            Additional arguments for the Devito constructor.

        Returns
        -------
        devito.TimeFunction
            Generated function.

        """
        space_order = space_order or self.space_order
        time_order = time_order or self.time_order

        fun = devito.TimeFunction(name=name,
                                  grid=self.grid,
                                  time_order=time_order,
                                  space_order=space_order,
                                  dtype=np.float32,
                                  **kwargs)

        return fun

    @_cached
    def undersampled_time_function(self, name, factor, space_order=None, time_order=None, **kwargs):
        """
        Create an undersampled version of a Devito function with parameters provided.

        Parameters
        ----------
        name : str
            Name of the function.
        factor : int,=
            Undersampling factor.
        space_order : int, optional
            Space order of the discretisation, defaults to the grid space order.
        time_order : int, optional
            Time order of the discretisation, defaults to the grid time order.
        kwargs
            Additional arguments for the Devito constructor.

        Returns
        -------
        devito.Function
            Generated function.

        """
        time = self._problem.time

        time_under = devito.ConditionalDimension('time_under',
                                                 parent=self.grid.time_dim,
                                                 factor=factor)

        buffer_size = (time.extended_num + factor - 1) // factor

        return self.time_function(name,
                                  space_order=space_order,
                                  time_order=time_order,
                                  time_dim=time_under,
                                  save=buffer_size,
                                  **kwargs)

    def deallocate(self, name):
        """
        Remove internal references to data buffers, if ``name`` is cached.

        Parameters
        ----------
        name : str
            Name of the function.

        Returns
        -------

        """
        if name in self.vars:
            del self.vars[name]._data
            self.vars[name]._data = None
            gc.collect()

    def with_halo(self, data):
        """
        Pad ndarray with appropriate halo given the grid space order.

        Parameters
        ----------
        data : ndarray
            Array to pad

        Returns
        -------
        ndarray
            Padded array.

        """
        pad_widths = [[self.space_order, self.space_order]
                      for _ in self._problem.space.shape]

        return np.pad(data, pad_widths, mode='edge')


class OperatorDevito:
    """
    Instances of this class encapsulate Devito operators, how to configure them and how to run them.


    Parameters
    ----------
    space_order : int
        Default space order of the discretisation for functions of the grid.
    time_order : int
        Default time order of the discretisation for functions of the grid.
    grid : GridDevito, optional
        Predefined GridDevito. A new one will be created unless specified.
    """

    def __init__(self, space_order, time_order, grid=None):
        self._problem = None

        self.operator = None
        self.kwargs = {}

        self.space_order = space_order
        self.time_order = time_order

        if grid is None:
            self.grid = GridDevito(space_order, time_order)
        else:
            self.grid = grid

        devito_logger = logging.getLogger('devito')
        devito.logger.logger = devito_logger

        class RerouteFilter(logging.Filter):

            def __init__(self):
                super().__init__()

            def filter(self, record):
                _runtime = mosaic.runtime()

                if record.levelno == devito.logger.PERF:
                    _runtime.logger.info(record.msg)

                elif record.levelno == logging.ERROR:
                    _runtime.logger.error(record.msg)

                elif record.levelno == logging.WARNING:
                    _runtime.logger.warning(record.msg)

                elif record.levelno == logging.DEBUG:
                    _runtime.logger.debug(record.msg)

                else:
                    _runtime.logger.info(record.msg)

                return False

        devito_logger.addFilter(RerouteFilter())

        runtime = mosaic.runtime()
        if runtime.mode == 'local':
            devito_logger.propagate = False

    def set_problem(self, problem):
        """
        Set up the problem or sub-problem that will be run with this operator.

        Parameters
        ----------
        problem : SubProblem or Problem
            Problem on which the physics will be executed

        Returns
        -------

        """
        self._problem = problem

    def set_operator(self, op, name='kernel', **kwargs):
        """
        Set up a Devito operator from a list of operations.

        Parameters
        ----------
        op : list
            List of operations to be given to the devito.Operator instance.
        name : str
            Name to give to the operator, defaults to ``kernel``.
        kwargs : optional
            Configuration parameters to set for Devito overriding defaults.

        Returns
        -------

        """
        default_config = {
            'autotuning': ['aggressive', 'runtime'],
            'develop-mode': False,
            'mpi': False,
            'log-level': 'DEBUG',
        }

        for key, value in default_config.items():
            if key in kwargs:
                value = kwargs[key]
                default_config[key] = value
                del kwargs[key]

            devito.parameters.configuration[key] = value

        default_kwargs = {
            'name': name,
            'subs': self.grid.grid.spacing_map,
            'opt': 'advanced',
            'platform': os.getenv('DEVITO_PLATFORM', None),
            'language': os.getenv('DEVITO_LANGUAGE', 'openmp'),
            'compiler': os.getenv('DEVITO_COMPILER', None),
        }

        default_kwargs.update(kwargs)

        runtime = mosaic.runtime()
        runtime.logger.info('Operator `%s` configuration:' % name)

        for key, value in default_config.items():
            runtime.logger.info('\t * %s=%s' % (key, value))

        for key, value in default_kwargs.items():
            if key == 'name':
                continue

            runtime.logger.info('\t * %s=%s' % (key, value))

        self.operator = devito.Operator(op, **default_kwargs)

    def compile(self):
        """
        Compile the operator.

        Returns
        -------

        """
        # compiler_flags = os.getenv('DEVITO_COMP_FLAGS', '').split(',')
        # compiler_flags = [each.strip() for each in compiler_flags]
        # self.operator._compiler.cflags += compiler_flags
        self.operator.cfunction

    def arguments(self, **kwargs):
        """
        Prepare Devito arguments.

        Parameters
        ----------
        kwargs : optional
            Arguments to pass to Devito.

        Returns
        -------

        """
        time = self._problem.time

        kwargs['time_m'] = kwargs.get('time_m', 0)
        kwargs['time_M'] = kwargs.get('time_M', time.extended_num - 1)

        self.kwargs.update(kwargs)

    def run(self):
        """
        Run the operator.

        Returns
        -------

        """
        self.operator.apply(**self.kwargs)
