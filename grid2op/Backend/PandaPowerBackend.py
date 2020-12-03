# Copyright (c) 2019-2020, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of Grid2Op, Grid2Op a testbed platform to model sequential decision making in power systems.

import os  # load the python os default module
import sys  # laod the python sys default module
import copy
import warnings

import numpy as np
import pandas as pd

import pandapower as pp
import scipy

from grid2op.dtypes import dt_int, dt_float, dt_bool
from grid2op.Backend.Backend import Backend
from grid2op.Action import BaseAction
from grid2op.Exceptions import *

try:
    import numba
    numba_ = True
except (ImportError, ModuleNotFoundError):
    numba_ = False
    warnings.warn("Numba cannot be loaded. You will gain possibly massive speed if installing it by "
                  "\n\t{} -m pip install numba\n".format(sys.executable))


class PandaPowerBackend(Backend):
    """
    .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        If you want to code a backend to use grid2op with another powerflow, you can get inspired
        from this class. Note However that implies knowning the behaviour
        of PandaPower.

    This module presents an example of an implementation of a `grid2op.Backend` when using the powerflow
    implementation "pandapower" available at `PandaPower <https://www.pandapower.org/>`_ for more details about
    this backend. This file is provided as an example of a proper :class:`grid2op.Backend.Backend` implementation.

    This backend currently does not work with 3 winding transformers and other exotic object.

    As explained in the `grid2op.Backend` module, every module must inherit the `grid2op.Backend` class.

    This class have more attributes that are used internally for faster information retrieval.

    Attributes
    ----------
    prod_pu_to_kv: :class:`numpy.array`, dtype:float
        The ratio that allow the conversion from pair-unit to kv for the generators

    load_pu_to_kv: :class:`numpy.array`, dtype:float
        The ratio that allow the conversion from pair-unit to kv for the loads

    lines_or_pu_to_kv: :class:`numpy.array`, dtype:float
        The ratio that allow the conversion from pair-unit to kv for the origin end of the powerlines

    lines_ex_pu_to_kv: :class:`numpy.array`, dtype:float
        The ratio that allow the conversion from pair-unit to kv for the extremity end of the powerlines

    p_or: :class:`numpy.array`, dtype:float
        The active power flowing at the origin end of each powerline

    q_or: :class:`numpy.array`, dtype:float
        The reactive power flowing at the origin end of each powerline

    v_or: :class:`numpy.array`, dtype:float
        The voltage magnitude at the origin bus of the powerline

    a_or: :class:`numpy.array`, dtype:float
        The current flowing at the origin end of each powerline

    p_ex: :class:`numpy.array`, dtype:float
        The active power flowing at the extremity end of each powerline

    q_ex: :class:`numpy.array`, dtype:float
        The reactive power flowing at the extremity end of each powerline

    a_ex: :class:`numpy.array`, dtype:float
        The current flowing at the extremity end of each powerline

    v_ex: :class:`numpy.array`, dtype:float
        The voltage magnitude at the extremity bus of the powerline

    Examples
    ---------
    The only recommended way to use this class is by passing an instance of a Backend into the "make"
    function of grid2op. Do not attempt to use a backend outside of this specific usage.

    .. code-block:: python

            import grid2op
            from grid2op.Backend import PandaPowerBackend
            backend = PandaPowerBackend()

            env = grid2op.make(backend=backend)
            # and use "env" as any open ai gym environment.

    """
    def __init__(self, detailed_infos_for_cascading_failures=False):
        Backend.__init__(self, detailed_infos_for_cascading_failures=detailed_infos_for_cascading_failures)
        self.prod_pu_to_kv = None
        self.load_pu_to_kv = None
        self.lines_or_pu_to_kv = None
        self.lines_ex_pu_to_kv = None

        self.p_or = None
        self.q_or = None
        self.v_or = None
        self.a_or = None
        self.p_ex = None
        self.q_ex = None
        self.v_ex = None
        self.a_ex = None

        self.load_p = None
        self.load_q = None
        self.load_v = None

        self.prod_p = None
        self.prod_q = None
        self.prod_v = None
        self.line_status = None

        self._pf_init = "flat"
        self._pf_init = "results"
        self._nb_bus_before = None  # number of active bus at the preceeding step

        self.thermal_limit_a = None

        self._iref_slack = None
        self._id_bus_added = None
        self._fact_mult_gen = -1
        self._what_object_where = None
        self._number_true_line = -1
        self._corresp_name_fun = {}
        self._get_vector_inj = {}
        self.dim_topo = -1
        self._vars_action = BaseAction.attr_list_vect
        self._vars_action_set = BaseAction.attr_list_vect
        self.cst_1 = dt_float(1.0)
        self._topo_vect = None
        self.slack_id = None
        self.comp_time = 0.

        # function to rstore some information
        self.__nb_bus_before = None  # number of substation in the powergrid
        self.__nb_powerline = None  # number of powerline (real powerline, not transformer)
        self._init_bus_load = None
        self._init_bus_gen = None
        self._init_bus_lor = None
        self._init_bus_lex = None
        self._get_vector_inj = None
        self._big_topo_to_obj = None
        self._big_topo_to_backend = None
        self.__pp_backend_initial_state = None  # initial state to facilitate the "reset"

        # Mapping some fun to apply bus updates
        self._type_to_bus_set = [
            self._apply_load_bus,
            self._apply_gen_bus,
            self._apply_lor_bus,
            self._apply_trafo_hv,
            self._apply_lex_bus,
            self._apply_trafo_lv
        ]

    def get_nb_active_bus(self):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        Compute the amount of buses "in service" eg with at least a powerline connected to it.

        Returns
        -------
        res: :class:`int`
            The total number of active buses.
        """
        return np.sum(self._grid.bus["in_service"])

    @staticmethod
    def _load_grid_load_p_mw(grid):
        return grid.load["p_mw"]

    @staticmethod
    def _load_grid_load_q_mvar(grid):
        return grid.load["q_mvar"]

    @staticmethod
    def _load_grid_gen_p_mw(grid):
        return grid.gen["p_mw"]

    @staticmethod
    def _load_grid_gen_vm_pu(grid):
        return grid.gen["vm_pu"]

    def reset(self, path=None, grid_filename=None):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        Reload the grid.
        For pandapower, it is a bit faster to store of a copy of itself at the end of load_grid
        and deep_copy it to itself instead of calling load_grid again
        """
        # Assign the content of itself as saved at the end of load_grid
        # This overide all the attributes with the attributes from the copy in __pp_backend_initial_state
        # self.__dict__.update(copy.deepcopy(self.__pp_backend_initial_state).__dict__)
        self._grid = copy.deepcopy(self.__pp_backend_initial_state._grid)
        self._reset_all_nan()
        self._topo_vect[:] = self._get_topo_vect()
        self.comp_time = 0.

    def load_grid(self, path=None, filename=None):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        Load the _grid, and initialize all the member of the class. Note that in order to perform topological
        modification of the substation of the underlying powergrid, some buses are added to the test case loaded. They
        are set as "out of service" unless a topological action acts on these specific substations.

        """

        if path is None and filename is None:
            raise RuntimeError("You must provide at least one of path or file to load a powergrid.")
        if path is None:
            full_path = filename
        elif filename is None:
            full_path = path
        else:
            full_path = os.path.join(path, filename)
        if not os.path.exists(full_path):
            raise RuntimeError("There is no powergrid at \"{}\"".format(full_path))

        with warnings.catch_warnings():
            # remove deprecationg warnings for old version of pandapower
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            self._grid = pp.from_json(full_path)

        # add the slack bus that is often not modeled as a generator, but i need it for this backend to work
        bus_gen_added = None
        i_ref = None
        self._iref_slack = None
        self._id_bus_added = None
        pp.runpp(self._grid, numba=numba_)
        if np.all(~self._grid.gen["slack"]):
            # there are not defined slack bus on the data, i need to hack it up a little bit
            pd2ppc = self._grid._pd2ppc_lookups["bus"]  # pd2ppc[pd_id] = ppc_id
            ppc2pd = np.argsort(pd2ppc)  # ppc2pd[ppc_id] = pd_id
            for i, el in enumerate(self._grid._ppc['gen'][:, 0]):
                if int(el) not in self._grid._pd2ppc_lookups["bus"][self._grid.gen["bus"].values]:
                    if bus_gen_added is not None:
                        raise RuntimeError("Impossible to recognize the powergrid")
                    bus_gen_added = ppc2pd[int(el)]
                    i_ref = i
                    break
            self._iref_slack = i_ref
            self._id_bus_added = self._grid.gen.shape[0]
            # see https://matpower.org/docs/ref/matpower5.0/idx_gen.html for details on the comprehension of self._grid._ppc
            pp.create_gen(self._grid, bus_gen_added,
                          p_mw=self._grid._ppc['gen'][i_ref, 1],
                          vm_pu=self._grid._ppc['gen'][i_ref, 5],
                          min_p_mw=self._grid._ppc['gen'][i_ref, 9],
                          max_p_mw=self._grid._ppc['gen'][i_ref, 8],
                          max_q_mvar=self._grid._ppc['gen'][i_ref, 3],
                          min_q_mvar=self._grid._ppc['gen'][i_ref, 4],
                          slack=True,
                          controllable=True)
        else:
            self.slack_id = np.where(self._grid.gen["slack"])[0]

        pp.runpp(self._grid, numba=numba_)
        self.__nb_bus_before = self._grid.bus.shape[0]
        self.__nb_powerline = self._grid.line.shape[0]
        self._init_bus_load = self.cst_1 * self._grid.load["bus"].values
        self._init_bus_gen = self.cst_1 * self._grid.gen["bus"].values
        self._init_bus_lor = self.cst_1 * self._grid.line["from_bus"].values
        self._init_bus_lex = self.cst_1 * self._grid.line["to_bus"].values

        t_for = self.cst_1 * self._grid.trafo["hv_bus"].values
        t_fex = self.cst_1 * self._grid.trafo["lv_bus"].values
        self._init_bus_lor = np.concatenate((self._init_bus_lor, t_for)).astype(np.int)
        self._init_bus_lex = np.concatenate((self._init_bus_lex, t_fex)).astype(np.int)

        self._grid["ext_grid"]["va_degree"] = 0.0

        # this has the effect to divide by 2 the active power in the added generator, if this generator and the "slack bus"
        # one are connected to the same bus.
        # if not, it must not be done. So basically, i create a vector for which p and q for generator must be multiply
        self._fact_mult_gen = np.ones(self._grid.gen.shape[0])
        # self._fact_mult_gen[-1] += 1

        # now extract the powergrid
        self.n_line = copy.deepcopy(self._grid.line.shape[0]) + copy.deepcopy(self._grid.trafo.shape[0])
        if "name" in self._grid.line.columns and not self._grid.line["name"].isnull().values.any():
            self.name_line = [name for name in self._grid.line["name"]]
        else:
            self.name_line = ['{from_bus}_{to_bus}_{id_powerline_me}'.format(**row, id_powerline_me=i)
                              for i, (_, row) in enumerate(self._grid.line.iterrows())]
        if "name" in self._grid.trafo.columns and not self._grid.trafo["name"].isnull().values.any():
            self.name_line += [name_traf for name_traf in self._grid.trafo["name"]]
        else:
            transfo = [('{hv_bus}'.format(**row), '{lv_bus}'.format(**row))
                       for i, (_, row) in enumerate(self._grid.trafo.iterrows())]
            transfo = [sorted(el) for el in transfo]
            self.name_line += ['{}_{}_{}'.format(*el, i + self._grid.line.shape[0]) for i, el in enumerate(transfo)]
        self.name_line = np.array(self.name_line)

        self.n_gen = copy.deepcopy(self._grid.gen.shape[0])
        if "name" in self._grid.gen.columns and not self._grid.gen["name"].isnull().values.any():
            self.name_gen = [name_g for name_g in self._grid.gen["name"]]
        else:
            self.name_gen = ["gen_{bus}_{index_gen}".format(**row, index_gen=i)
                             for i, (_, row) in enumerate(self._grid.gen.iterrows())]
        self.name_gen = np.array(self.name_gen)

        self.n_load = copy.deepcopy(self._grid.load.shape[0])
        if "name" in self._grid.load.columns and not self._grid.load["name"].isnull().values.any():
            self.name_load = [nl for nl in self._grid.load["name"]]
        else:
            self.name_load = ["load_{bus}_{index_gen}".format(**row, index_gen=i)
                              for i, (_, row) in enumerate(self._grid.load.iterrows())]
        self.name_load = np.array(self.name_load)
        self.n_sub = copy.deepcopy(self._grid.bus.shape[0])
        self.name_sub = ["sub_{}".format(i) for i, row in self._grid.bus.iterrows()]
        self.name_sub = np.array(self.name_sub)
        #  number of elements per substation
        self.sub_info = np.zeros(self.n_sub, dtype=dt_int)

        self.load_to_subid = np.zeros(self.n_load, dtype=dt_int)
        self.gen_to_subid = np.zeros(self.n_gen, dtype=dt_int)
        self.line_or_to_subid = np.zeros(self.n_line, dtype=dt_int)
        self.line_ex_to_subid = np.zeros(self.n_line, dtype=dt_int)

        self.load_to_sub_pos = np.zeros(self.n_load, dtype=dt_int)
        self.gen_to_sub_pos = np.zeros(self.n_gen, dtype=dt_int)
        self.line_or_to_sub_pos = np.zeros(self.n_line, dtype=dt_int)
        self.line_ex_to_sub_pos = np.zeros(self.n_line, dtype=dt_int)

        pos_already_used = np.zeros(self.n_sub, dtype=dt_int)
        self._what_object_where = [[] for _ in range(self.n_sub)]

        # self._grid.line.sort_index(inplace=True)
        # self._grid.trafo.sort_index(inplace=True)
        # self._grid.gen.sort_index(inplace=True)
        # self._grid.load.sort_index(inplace=True)

        for i, (_, row) in enumerate(self._grid.line.iterrows()):
            sub_or_id = int(row["from_bus"])
            sub_ex_id = int(row["to_bus"])
            self.sub_info[sub_or_id] += 1
            self.sub_info[sub_ex_id] += 1
            self.line_or_to_subid[i] = sub_or_id
            self.line_ex_to_subid[i] = sub_ex_id

            self.line_or_to_sub_pos[i] = pos_already_used[sub_or_id]
            pos_already_used[sub_or_id] += 1
            self.line_ex_to_sub_pos[i] = pos_already_used[sub_ex_id]
            pos_already_used[sub_ex_id] += 1

            self._what_object_where[sub_or_id].append(("line", "from_bus", i))
            self._what_object_where[sub_ex_id].append(("line", "to_bus", i))

        lag_transfo = self._grid.line.shape[0]
        self._number_true_line = copy.deepcopy(self._grid.line.shape[0])
        for i, (_, row) in enumerate(self._grid.trafo.iterrows()):
            sub_or_id = int(row["hv_bus"])
            sub_ex_id = int(row["lv_bus"])
            self.sub_info[sub_or_id] += 1
            self.sub_info[sub_ex_id] += 1
            self.line_or_to_subid[i + lag_transfo] = sub_or_id
            self.line_ex_to_subid[i + lag_transfo] = sub_ex_id

            self.line_or_to_sub_pos[i + lag_transfo] = pos_already_used[sub_or_id]
            pos_already_used[sub_or_id] += 1
            self.line_ex_to_sub_pos[i + lag_transfo] = pos_already_used[sub_ex_id]
            pos_already_used[sub_ex_id] += 1

            self._what_object_where[sub_or_id].append(("trafo", "hv_bus", i))
            self._what_object_where[sub_ex_id].append(("trafo", "lv_bus", i))

        for i, (_, row) in enumerate(self._grid.gen.iterrows()):
            sub_id = int(row["bus"])
            self.sub_info[sub_id] += 1
            self.gen_to_subid[i] = sub_id
            self.gen_to_sub_pos[i] = pos_already_used[sub_id]
            pos_already_used[sub_id] += 1

            self._what_object_where[sub_id].append(("gen", "bus", i))

        for i, (_, row) in enumerate(self._grid.load.iterrows()):
            sub_id = int(row["bus"])
            self.sub_info[sub_id] += 1
            self.load_to_subid[i] = sub_id
            self.load_to_sub_pos[i] = pos_already_used[sub_id]
            pos_already_used[sub_id] += 1

            self._what_object_where[sub_id].append(("load", "bus", i))

        self.dim_topo = np.sum(self.sub_info)
        self._compute_pos_big_topo()

        # utilities for imeplementing apply_action
        self._corresp_name_fun = {}

        self._get_vector_inj = {}
        self._get_vector_inj["load_p"] = self._load_grid_load_p_mw #lambda grid: grid.load["p_mw"]
        self._get_vector_inj["load_q"] = self._load_grid_load_q_mvar #lambda grid: grid.load["q_mvar"]
        self._get_vector_inj["prod_p"] = self._load_grid_gen_p_mw #lambda grid: grid.gen["p_mw"]
        self._get_vector_inj["prod_v"] = self._load_grid_gen_vm_pu #lambda grid: grid.gen["vm_pu"]

        # "hack" to handle topological changes, for now only 2 buses per substation
        add_topo = copy.deepcopy(self._grid.bus)
        add_topo.index += add_topo.shape[0]
        add_topo["in_service"] = False
        self._grid.bus = pd.concat((self._grid.bus, add_topo))

        self.load_pu_to_kv = self._grid.bus["vn_kv"][self.load_to_subid].values.astype(dt_float)
        self.prod_pu_to_kv = self._grid.bus["vn_kv"][self.gen_to_subid].values.astype(dt_float)
        self.lines_or_pu_to_kv = self._grid.bus["vn_kv"][self.line_or_to_subid].values.astype(dt_float)
        self.lines_ex_pu_to_kv = self._grid.bus["vn_kv"][self.line_ex_to_subid].values.astype(dt_float)

        self.thermal_limit_a = 1000 * np.concatenate((self._grid.line["max_i_ka"].values,
                                                      self._grid.trafo["sn_mva"].values / (np.sqrt(3) * self._grid.trafo["vn_hv_kv"].values)))
        self.thermal_limit_a = self.thermal_limit_a.astype(dt_float)

        self.p_or = np.full(self.n_line, dtype=dt_float, fill_value=np.NaN)
        self.q_or = np.full(self.n_line, dtype=dt_float, fill_value=np.NaN)
        self.v_or = np.full(self.n_line, dtype=dt_float, fill_value=np.NaN)
        self.a_or = np.full(self.n_line, dtype=dt_float, fill_value=np.NaN)
        self.p_ex = np.full(self.n_line, dtype=dt_float, fill_value=np.NaN)
        self.q_ex = np.full(self.n_line, dtype=dt_float, fill_value=np.NaN)
        self.v_ex = np.full(self.n_line, dtype=dt_float, fill_value=np.NaN)
        self.a_ex = np.full(self.n_line, dtype=dt_float, fill_value=np.NaN)
        self.line_status = np.full(self.n_line, dtype=dt_bool, fill_value=np.NaN)
        self.load_p = np.full(self.n_load, dtype=dt_float, fill_value=np.NaN)
        self.load_q = np.full(self.n_load, dtype=dt_float, fill_value=np.NaN)
        self.load_v = np.full(self.n_load, dtype=dt_float, fill_value=np.NaN)
        self.prod_p = np.full(self.n_gen, dtype=dt_float, fill_value=np.NaN)
        self.prod_v = np.full(self.n_gen, dtype=dt_float, fill_value=np.NaN)
        self.prod_q = np.full(self.n_gen, dtype=dt_float, fill_value=np.NaN)
        self._nb_bus_before = None

        # shunts data
        self.n_shunt = self._grid.shunt.shape[0]
        self.shunt_to_subid = np.zeros(self.n_shunt, dtype=dt_int) - 1
        name_shunt = []
        for i, (_, row) in enumerate(self._grid.shunt.iterrows()):
            bus = int(row["bus"])
            name_shunt.append("shunt_{bus}_{index_shunt}".format(**row, index_shunt=i))
            self.shunt_to_subid[i] = bus
        self.name_shunt = np.array(name_shunt)
        self._sh_vnkv = self._grid.bus["vn_kv"][self.shunt_to_subid].values.astype(dt_float)
        self.shunts_data_available = True

        # store the topoid -> objid
        self._big_topo_to_obj = [(None, None) for _ in range(self.dim_topo)]
        nm_ = "load"
        for load_id, pos_big_topo  in enumerate(self.load_pos_topo_vect):
            self._big_topo_to_obj[pos_big_topo] = (load_id, nm_)
        nm_ = "gen"
        for gen_id, pos_big_topo  in enumerate(self.gen_pos_topo_vect):
            self._big_topo_to_obj[pos_big_topo] = (gen_id, nm_)
        nm_ = "lineor"
        for l_id, pos_big_topo in enumerate(self.line_or_pos_topo_vect):
            self._big_topo_to_obj[pos_big_topo] = (l_id, nm_)
        nm_ = "lineex"
        for l_id, pos_big_topo  in enumerate(self.line_ex_pos_topo_vect):
            self._big_topo_to_obj[pos_big_topo] = (l_id, nm_)

        # store the topoid -> objid
        self._big_topo_to_backend = [(None, None, None) for _ in range(self.dim_topo)]
        for load_id, pos_big_topo in enumerate(self.load_pos_topo_vect):
            self._big_topo_to_backend[pos_big_topo] = (load_id, load_id, 0)
        for gen_id, pos_big_topo  in enumerate(self.gen_pos_topo_vect):
            self._big_topo_to_backend[pos_big_topo] = (gen_id, gen_id, 1)
        for l_id, pos_big_topo in enumerate(self.line_or_pos_topo_vect):
            if l_id < self.__nb_powerline:
                self._big_topo_to_backend[pos_big_topo] = (l_id, l_id, 2)
            else:
                self._big_topo_to_backend[pos_big_topo] = (l_id, l_id - self.__nb_powerline, 3)
        for l_id, pos_big_topo  in enumerate(self.line_ex_pos_topo_vect):
            if l_id < self.__nb_powerline:
                self._big_topo_to_backend[pos_big_topo] = (l_id, l_id, 4)
            else:
                self._big_topo_to_backend[pos_big_topo] = (l_id, l_id - self.__nb_powerline, 5)

        self._topo_vect = self._get_topo_vect()
        # Create a deep copy of itself in the initial state
        pp_backend_initial_state = copy.deepcopy(self)
        # Store it under super private attribute
        self.__pp_backend_initial_state = pp_backend_initial_state

    def _convert_id_topo(self, id_big_topo):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        convert an id of the big topo vector into:

        - the id of the object in its "only object" (eg if id_big_topo represents load 2, then it will be 2)
        - the type of object among: "load", "gen", "lineor" and "lineex"

        """
        return self._big_topo_to_obj[id_big_topo]

    def apply_action(self, backendAction=None):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        Specific implementation of the method to apply an action modifying a powergrid in the pandapower format.
        """
        if backendAction is None:
            return

        active_bus, (prod_p, prod_v, load_p, load_q), topo__, shunts__ = backendAction()

        tmp_prod_p = self._get_vector_inj["prod_p"](self._grid)
        if np.any(prod_p.changed):
            tmp_prod_p.iloc[prod_p.changed] = prod_p.values[prod_p.changed]

        tmp_prod_v = self._get_vector_inj["prod_v"](self._grid)
        if np.any(prod_v.changed):
            tmp_prod_v.iloc[prod_v.changed] = prod_v.values[prod_v.changed] / self.prod_pu_to_kv[prod_v.changed]

        if self._id_bus_added is not None and prod_v.changed[self._id_bus_added]:
            # handling of the slack bus, where "2" generators are present.
            self._grid["ext_grid"]["vm_pu"] = 1.0 * tmp_prod_v[self._id_bus_added]

        tmp_load_p = self._get_vector_inj["load_p"](self._grid)
        if np.any(load_p.changed):
            tmp_load_p.iloc[load_p.changed] = load_p.values[load_p.changed]

        tmp_load_q = self._get_vector_inj["load_q"](self._grid)
        if np.any(load_q.changed):
            tmp_load_q.iloc[load_q.changed] = load_q.values[load_q.changed]

        if self.shunts_data_available:
            shunt_p, shunt_q, shunt_bus = shunts__

            if np.any(shunt_p.changed):
                self._grid.shunt["p_mw"].iloc[shunt_p.changed] = shunt_p.values[shunt_p.changed]
            if np.any(shunt_q.changed):
                self._grid.shunt["q_mvar"].iloc[shunt_q.changed] = shunt_q.values[shunt_q.changed]
            if np.any(shunt_bus.changed):
                sh_service = shunt_bus.values[shunt_bus.changed] != -1
                self._grid.shunt["in_service"].iloc[shunt_bus.changed] = sh_service
                sh_bus1 = np.arange(len(shunt_bus))[shunt_bus.changed & shunt_bus.values == 1]
                sh_bus2 = np.arange(len(shunt_bus))[shunt_bus.changed & shunt_bus.values == 2]
                if len(sh_bus1) > 0:
                    self._grid.shunt["bus"][sh_bus1] = self.shunt_to_subid[sh_bus1]
                if len(sh_bus2) > 0:
                    self._grid.shunt["bus"][sh_bus2] = self.shunt_to_subid[sh_bus2] + self.__nb_bus_before

        # i made at least a real change, so i implement it in the backend
        for id_el, new_bus in topo__:
            id_el_backend, id_topo, type_obj = self._big_topo_to_backend[id_el]
            self._type_to_bus_set[type_obj](new_bus, id_el_backend, id_topo)

        bus_is = self._grid.bus["in_service"]
        for i, (bus1_status, bus2_status) in enumerate(active_bus):
            bus_is[i] = bus1_status  # no iloc for bus, don't ask me why please :-/
            bus_is[i + self.__nb_bus_before] = bus2_status

    def _apply_load_bus(self, new_bus, id_el_backend, id_topo):
        new_bus_backend = self._pp_bus_from_grid2op_bus(new_bus, self._init_bus_load[id_el_backend])
        if new_bus_backend >= 0:
            self._grid.load["bus"].iat[id_el_backend] = new_bus_backend
            self._grid.load["in_service"].iat[id_el_backend] = True
        else:
            self._grid.load["in_service"].iat[id_el_backend] = False
            self._grid.load["bus"].iat[id_el_backend] = -1

    def _apply_gen_bus(self, new_bus, id_el_backend, id_topo):
        new_bus_backend = self._pp_bus_from_grid2op_bus(new_bus, self._init_bus_gen[id_el_backend])
        if new_bus_backend >= 0:
            self._grid.gen["bus"].iat[id_el_backend] = new_bus_backend
            self._grid.gen["in_service"].iat[id_el_backend] = True
            # remember in this case slack bus is actually 2 generators for pandapower !
            if id_el_backend == (self._grid.gen.shape[0] - 1) and self._iref_slack is not None:
                self._grid.ext_grid["bus"].iat[0] = new_bus_backend
        else:
            self._grid.gen["in_service"].iat[id_el_backend] = False
            self._grid.gen["bus"].iat[id_el_backend] = -1
            # in this case the slack bus cannot be disconnected

    def _apply_lor_bus(self, new_bus, id_el_backend, id_topo):
        new_bus_backend = self._pp_bus_from_grid2op_bus(new_bus, self._init_bus_lor[id_el_backend])
        self.change_bus_powerline_or(id_el_backend, new_bus_backend)

    def change_bus_powerline_or(self, id_powerline_backend, new_bus_backend):
        if new_bus_backend >= 0:
            self._grid.line["in_service"].iat[id_powerline_backend] = True
            self._grid.line["from_bus"].iat[id_powerline_backend] = new_bus_backend
        else:
            self._grid.line["in_service"].iat[id_powerline_backend] = False

    def _apply_lex_bus(self, new_bus, id_el_backend, id_topo):
        new_bus_backend = self._pp_bus_from_grid2op_bus(new_bus, self._init_bus_lex[id_el_backend])
        self.change_bus_powerline_ex(id_el_backend, new_bus_backend)

    def change_bus_powerline_ex(self, id_powerline_backend, new_bus_backend):
        if new_bus_backend >= 0:
            self._grid.line["in_service"].iat[id_powerline_backend] = True
            self._grid.line["to_bus"].iat[id_powerline_backend] = new_bus_backend
        else:
            self._grid.line["in_service"].iat[id_powerline_backend] = False

    def _apply_trafo_hv(self, new_bus, id_el_backend, id_topo):
        new_bus_backend = self._pp_bus_from_grid2op_bus(new_bus, self._init_bus_lor[id_el_backend])
        self.change_bus_trafo_hv(id_topo, new_bus_backend)

    def change_bus_trafo_hv(self, id_powerline_backend, new_bus_backend):
        if new_bus_backend >= 0:
            self._grid.trafo["in_service"].iat[id_powerline_backend] = True
            self._grid.trafo["hv_bus"].iat[id_powerline_backend] = new_bus_backend
        else:
            self._grid.trafo["in_service"].iat[id_powerline_backend] = False

    def _apply_trafo_lv(self, new_bus, id_el_backend, id_topo):
        new_bus_backend = self._pp_bus_from_grid2op_bus(new_bus, self._init_bus_lex[id_el_backend])
        self.change_bus_trafo_lv(id_topo, new_bus_backend)

    def change_bus_trafo_lv(self, id_powerline_backend, new_bus_backend):
        if new_bus_backend >= 0:
            self._grid.trafo["in_service"].iat[id_powerline_backend] = True
            self._grid.trafo["lv_bus"].iat[id_powerline_backend] = new_bus_backend
        else:
            self._grid.trafo["in_service"].iat[id_powerline_backend] = False

    def _pp_bus_from_grid2op_bus(self, grid2op_bus, grid2op_bus_init):
        if grid2op_bus == 1:
            res = grid2op_bus_init
        elif grid2op_bus == 2:
            res = grid2op_bus_init + self.__nb_bus_before
        elif grid2op_bus == -1:
            res = -1
        else:
            raise BackendError("grid2op bus must be -1, 1 or 2")
        return int(res)

    def _aux_get_line_info(self, colname1, colname2):
        res = np.concatenate((self._grid.res_line[colname1].values, self._grid.res_trafo[colname2].values))
        return res

    def runpf(self, is_dc=False):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        Run a power flow on the underlying _grid. This implements an optimization of the powerflow
        computation: if the number of
        buses has not changed between two calls, the previous results are re used. This speeds up the computation
        in case of "do nothing" action applied.
        """
        conv = True
        nb_bus = self.get_nb_active_bus()
        try:
            with warnings.catch_warnings():
                # remove the warning if _grid non connex. And it that case load flow as not converged
                warnings.filterwarnings("ignore", category=scipy.sparse.linalg.MatrixRankWarning)
                warnings.filterwarnings("ignore", category=RuntimeWarning)
                if self._nb_bus_before is None:
                    self._pf_init = "dc"
                elif nb_bus == self._nb_bus_before:
                    self._pf_init = "results"
                else:
                    self._pf_init = "auto"

                if np.any(~self._grid.load["in_service"]):
                    # TODO see if there is a better way here -> do not handle this here, but rather in Backend._next_grid_state
                    raise pp.powerflow.LoadflowNotConverged("Isolated load")
                if np.any(~self._grid.gen["in_service"]):
                    # TODO see if there is a better way here -> do not handle this here, but rather in Backend._next_grid_state
                    raise pp.powerflow.LoadflowNotConverged("Isolated gen")

                if is_dc:
                    pp.rundcpp(self._grid, check_connectivity=False)
                    self._nb_bus_before = None  # if dc i start normally next time i call an ac powerflow
                else:
                    pp.runpp(self._grid, check_connectivity=False, init=self._pf_init, numba=numba_)

                # stores the computation time
                if "_ppc" in self._grid:
                    if "et" in self._grid["_ppc"]:
                        self.comp_time += self._grid["_ppc"]["et"]
                if self._grid.res_gen.isnull().values.any():
                    # TODO see if there is a better way here -> do not handle this here, but rather in Backend._next_grid_state
                    # sometimes pandapower does not detect divergence and put Nan.
                    raise pp.powerflow.LoadflowNotConverged("Isolated gen")

                self.prod_p[:], self.prod_q[:], self.prod_v[:] = self._gens_info()
                self.load_p[:], self.load_q[:], self.load_v[:] = self._loads_info()
                if not is_dc:
                    if not np.all(np.isfinite(self.load_v)):
                        # TODO see if there is a better way here
                        # some loads are disconnected: it's a game over case!
                        raise pp.powerflow.LoadflowNotConverged("Isolated load")
                else:
                    # fix voltages magnitude that are always "nan" for dc case
                    # self._grid.res_bus["vm_pu"] is always nan when computed in DC
                    self.load_v[:] = self.load_pu_to_kv  # TODO
                    # need to assign the correct value when a generator is present at the same bus
                    # TODO optimize this ugly loop
                    for l_id in range(self.n_load):
                        if self.load_to_subid[l_id] in self.gen_to_subid:
                            ind_gens = np.where(self.gen_to_subid == self.load_to_subid[l_id])[0]
                            for g_id in ind_gens:
                                if self._topo_vect[self.load_pos_topo_vect[l_id]] == self._topo_vect[self.load_pos_topo_vect[g_id]]:
                                    self.load_v[l_id] = self.prod_v[g_id]
                                    break

                self.line_status[:] = self._get_line_status()
                # I retrieve the data once for the flows, so has to not re read multiple dataFrame
                self.p_or[:] = self._aux_get_line_info("p_from_mw", "p_hv_mw")
                self.q_or[:] = self._aux_get_line_info("q_from_mvar", "q_hv_mvar")
                self.v_or[:] = self._aux_get_line_info("vm_from_pu", "vm_hv_pu")
                self.a_or[:] = self._aux_get_line_info("i_from_ka", "i_hv_ka") * 1000
                self.a_or[~np.isfinite(self.a_or)] = 0.
                self.v_or[~np.isfinite(self.v_or)] = 0.

                self.p_ex[:] = self._aux_get_line_info("p_to_mw", "p_lv_mw")
                self.q_ex[:] = self._aux_get_line_info("q_to_mvar", "q_lv_mvar")
                self.v_ex[:] = self._aux_get_line_info("vm_to_pu", "vm_lv_pu")
                self.a_ex[:] = self._aux_get_line_info("i_to_ka", "i_lv_ka") * 1000
                self.a_ex[~np.isfinite(self.a_ex)] = 0.
                self.v_ex[~np.isfinite(self.v_ex)] = 0.

                # it seems that pandapower does not take into account disconencted powerline for their voltage
                self.v_or[~self.line_status] = 0.
                self.v_ex[~self.line_status] = 0.

                self.v_or[:] *= self.lines_or_pu_to_kv
                self.v_ex[:] *= self.lines_ex_pu_to_kv

                self._nb_bus_before = None
                self._grid._ppc["gen"][self._iref_slack, 1] = 0.
                self._topo_vect[:] = self._get_topo_vect()
                return self._grid.converged

        except pp.powerflow.LoadflowNotConverged as exc_:
            # of the powerflow has not converged, results are Nan
            self._reset_all_nan()
            return False

    def _reset_all_nan(self):
        self.p_or[:] = np.NaN
        self.q_or[:] = np.NaN
        self.v_or[:] = np.NaN
        self.a_or[:] = np.NaN
        self.p_ex[:] = np.NaN
        self.q_ex[:] = np.NaN
        self.v_ex[:] = np.NaN
        self.a_ex[:] = np.NaN
        self.prod_p[:] = np.NaN
        self.prod_q[:] = np.NaN
        self.prod_v[:] = np.NaN
        self.load_p[:] = np.NaN
        self.load_q[:] = np.NaN
        self.load_v[:] = np.NaN
        self._nb_bus_before = None

    def copy(self):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        Performs a deep copy of the power :attr:`_grid`.
        As pandapower is pure python, the deep copy operator is perfectly suited for the task.
        """
        res = copy.deepcopy(self)
        return res

    def close(self):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        Called when the :class:`grid2op;Environment` has terminated, this function only reset the grid to a state
        where it has not been loaded.
        """
        del self._grid
        self._grid = None

    def save_file(self, full_path):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        You might want to use it for debugging purpose only, and only if you develop yourself a backend.

        Save the file to json.
        :param full_path:
        :return:
        """
        pp.to_json(self._grid, full_path)

    def get_line_status(self):
        """
        .. warning:: /!\\\\ Internal, do not use unless you know what you are doing /!\\\\

        As all the functions related to powerline, pandapower split them into multiple dataframe (some for transformers,
        some for 3 winding transformers etc.). We make sure to get them all here.
        """
        return self.line_status

    def _get_line_status(self):
        return np.concatenate((self._grid.line["in_service"].values, self._grid.trafo["in_service"].values)).astype(dt_bool)

    def get_line_flow(self):
        return self.a_or

    def _disconnect_line(self, id_):
        if id_ < self._number_true_line:
            self._grid.line["in_service"].iloc[id_] = False
        else:
            self._grid.trafo["in_service"].iloc[id_ - self._number_true_line] = False
        self._topo_vect[self.line_or_pos_topo_vect[id_]] = -1
        self._topo_vect[self.line_ex_pos_topo_vect[id_]] = -1
        self.line_status[id_] = False

    def _reconnect_line(self, id_):
        if id_ < self._number_true_line:
            self._grid.line["in_service"].iloc[id_] = True
        else:
            self._grid.trafo["in_service"].iloc[id_ - self._number_true_line] = True
        self.line_status[id_] = True

    def get_topo_vect(self):
        return self._topo_vect

    def _get_topo_vect(self):
        res = np.full(self.dim_topo, fill_value=np.NaN, dtype=dt_int)

        line_status = self.get_line_status()

        i = 0
        for row in self._grid.line[["from_bus", "to_bus"]].values:
            bus_or_id = row[0]
            bus_ex_id = row[1]
            if line_status[i]:
                res[self.line_or_pos_topo_vect[i]] = 1 if bus_or_id == self.line_or_to_subid[i] else 2
                res[self.line_ex_pos_topo_vect[i]] = 1 if bus_ex_id == self.line_ex_to_subid[i] else 2
            else:
                res[self.line_or_pos_topo_vect[i]] = -1
                res[self.line_ex_pos_topo_vect[i]] = -1
            i += 1

        nb = self._number_true_line
        i = 0
        for row in self._grid.trafo[["hv_bus", "lv_bus"]].values:
            bus_or_id = row[0]
            bus_ex_id = row[1]

            j = i + nb
            if line_status[j]:
                res[self.line_or_pos_topo_vect[j]] = 1 if bus_or_id == self.line_or_to_subid[j] else 2
                res[self.line_ex_pos_topo_vect[j]] = 1 if bus_ex_id == self.line_ex_to_subid[j] else 2
            else:
                res[self.line_or_pos_topo_vect[j]] = -1
                res[self.line_ex_pos_topo_vect[j]] = -1
            i += 1

        i = 0
        for bus_id in self._grid.gen["bus"].values:
            res[self.gen_pos_topo_vect[i]] = 1 if bus_id == self.gen_to_subid[i] else 2
            i += 1

        i = 0
        for bus_id in self._grid.load["bus"].values:
            res[self.load_pos_topo_vect[i]] = 1 if bus_id == self.load_to_subid[i] else 2
            i += 1

        return res

    def _gens_info(self):
        prod_p = self.cst_1 * self._grid.res_gen["p_mw"].values.astype(dt_float)
        prod_q = self.cst_1 * self._grid.res_gen["q_mvar"].values.astype(dt_float)
        prod_v = self.cst_1 * self._grid.res_gen["vm_pu"].values.astype(dt_float) * self.prod_pu_to_kv
        if self._iref_slack is not None:
            # slack bus and added generator are on same bus. I need to add power of slack bus to this one.

            # if self._grid.gen["bus"].iloc[self._id_bus_added] == self.gen_to_subid[self._id_bus_added]:
            if "gen" in self._grid._ppc["internal"]:
                prod_p[self._id_bus_added] += self._grid._ppc["internal"]["gen"][self._iref_slack, 1]
                prod_q[self._id_bus_added] += self._grid._ppc["internal"]["gen"][self._iref_slack, 2]
        return prod_p, prod_q, prod_v

    def _loads_info(self):
        load_p = self.cst_1 * self._grid.res_load["p_mw"].values.astype(dt_float)
        load_q = self.cst_1 * self._grid.res_load["q_mvar"].values.astype(dt_float)
        load_v = self._grid.res_bus.loc[self._grid.load["bus"].values]["vm_pu"].values.astype(dt_float) * self.load_pu_to_kv
        return load_p, load_q, load_v

    def generators_info(self):
        return self.cst_1 * self.prod_p, self.cst_1 * self.prod_q, self.cst_1 * self.prod_v

    def loads_info(self):
        return self.cst_1 * self.load_p, self.cst_1 * self.load_q, self.cst_1 * self.load_v

    def lines_or_info(self):
        return self.cst_1 * self.p_or, self.cst_1 * self.q_or, self.cst_1 * self.v_or, self.cst_1 * self.a_or

    def lines_ex_info(self):
        return self.cst_1 * self.p_ex, self.cst_1 * self.q_ex, self.cst_1 * self.v_ex, self.cst_1 * self.a_ex

    def shunt_info(self):
        shunt_p = self.cst_1 * self._grid.res_shunt["p_mw"].values.astype(dt_float)
        shunt_q = self.cst_1 * self._grid.res_shunt["q_mvar"].values.astype(dt_float)
        shunt_v = self._grid.res_bus["vm_pu"].loc[self._grid.shunt["bus"].values].values.astype(dt_float)
        shunt_v *= self._grid.bus["vn_kv"].loc[self._grid.shunt["bus"].values].values.astype(dt_float)
        shunt_bus = self._grid.shunt["bus"].values < self.__nb_bus_before
        shunt_bus = 1 * shunt_bus
        shunt_bus = shunt_bus.astype(dt_int)
        return shunt_p, shunt_q, shunt_v, shunt_bus

    def sub_from_bus_id(self, bus_id):
        if bus_id >= self._number_true_line:
            return bus_id - self._number_true_line
        return bus_id
