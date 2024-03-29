# -*- coding: utf-8 -*-
# ===============LICENSE_START=======================================================
# Acumos Apache-2.0
# ===================================================================================
# Copyright (C) 2017-2018 AT&T Intellectual Property & Tech Mahindra. All rights reserved.
# ===================================================================================
# This Acumos software file is distributed by AT&T and Tech Mahindra
# under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# This file is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===============LICENSE_END=========================================================
"""
Provides custom pickle utilities
"""
import sys
import json
import inspect
import contextlib
import tempfile
from os import makedirs
from os.path import basename, isdir, isfile, join as path_join
from copy import deepcopy
from functools import partial
from typing import GenericMeta, Dict, List
from types import ModuleType
from importlib import import_module

import dill

from .modeling import _is_namedtuple, create_namedtuple, Empty
from .utils import namedtuple_field_types


_DEFAULT_MODULES = ('gcumos', 'dill')
_BLACKLIST = {'builtins', }
_DEFAULT = 'default'

_contexts = dict()

dump_model = partial(dill.dump, recurse=True)
dumps_model = partial(dill.dumps, recurse=True)
load_model = dill.load
loads_model = dill.loads


def _save_annotation(pickler, obj):
    '''Workaround for dill annotation serialization bug'''
    if obj.__origin__ in (Dict, List):
        # recursively save object
        t = obj.__origin__
        args = obj.__args__
        pickler.save_reduce(_load_annotation, (t, args), obj=obj)
    else:
        # eventually hit base type, then use stock pickling logic. temp revert prevents infinite recursion
        t = obj
        args = None
        with _revert_dispatch(GenericMeta):
            pickler.save_reduce(_load_annotation, (t, args), obj=obj)


def _load_annotation(t, args):
    '''Workaround for dill annotation serialization bug'''
    if t is Dict and args is not None:
        return Dict[args[0], args[1]]
    elif t is List and args is not None:
        return List[args[0]]
    else:
        return t


def _save_namedtuple(pickler, obj):
    '''Workaround for dill NamedTuple serialization bug'''
    field_types = namedtuple_field_types(obj)
    pickler.save_reduce(_load_namedtuple, (obj.__name__, field_types), obj=obj)


def _load_namedtuple(name, field_types):
    '''Workaround for dill NamedTuple serialization bug'''
    return create_namedtuple(name, [(k, v) for k, v in field_types.items()])


@contextlib.contextmanager
def _revert_dispatch(t):
    '''Temporarily removes a type from the dispatch table to use existing serialization logic'''
    f = dill.Pickler.dispatch.pop(t)
    try:
        yield
    finally:
        dill.Pickler.dispatch[t] = f


def _add_file(subdir, name):
    '''Helper function which returns the absolute and context-relative path of a file to be added'''
    file_abspath = path_join(subdir, name)
    file_relpath = (basename(subdir), name)
    return file_abspath, file_relpath


dill.Pickler.dispatch[GenericMeta] = _save_annotation


_CUSTOM_DISPATCH = {
}


@contextlib.contextmanager
def _patch_dill():
    '''Temporarily patches the dill Pickler dispatch table to support custom serialization within a context'''
    try:
        dispatch = dill.Pickler.dispatch
        dill.Pickler.dispatch = deepcopy(dispatch)

        pickler_save = dill.Pickler.save

        def wrapped_save(pickler, obj, save_persistent_id=True):
            '''Hook that intercepts objects about to be saved'''
            _catch_object(obj)

            if _is_namedtuple(obj) and obj is not Empty:
                _save_namedtuple(pickler, obj)
            else:
                pickler_save(pickler, obj, save_persistent_id)

        dill.Pickler.save = wrapped_save
        yield
    finally:
        dill.Pickler.dispatch = dispatch
        dill.Pickler.save = pickler_save


def _catch_object(obj):
    '''Inspects object and executes custom serialization / bookkeeping logic'''

    # dynamically extend dispatch table to prevent unnecessary imports / dependencies
    obj_type = obj if inspect.isclass(obj) else type(obj)
    if obj_type not in dill.Pickler.dispatch:
        for path in _get_mro_paths(obj_type):
            if path in _CUSTOM_DISPATCH:
                dill.Pickler.dispatch[obj_type] = _CUSTOM_DISPATCH[path]

    base_module = _get_base_module(obj)
    if base_module is not None and base_module.__name__ not in _BLACKLIST:
        context = get_context()
        context.add_module(base_module)


def _get_base_module(obj):
    '''Returns the base module for a given object'''
    module = inspect.getmodule(obj)
    if module is not None:
        base_name, _, _ = module.__name__.partition('.')
        base_module = sys.modules[base_name]
    else:
        base_module = None
    return base_module


def _get_mro_paths(type_):
    '''Yields import path string for each entry in `inspect.getmro`'''
    for t in inspect.getmro(type_):
        yield "{}.{}".format(t.__module__, t.__name__)


class AcumosContext(object):
    '''Represents a workspace for a model that is being dumped'''

    def __init__(self, root_dir):
        if not isdir(root_dir):
            raise Exception("AcumosContext root directory {} does not exist".format(root_dir))
        self._modules = set()
        self._root_dir = root_dir
        self._params_path = path_join(root_dir, 'context.json')
        self.parameters = self._load_params()

        for mod in _DEFAULT_MODULES:
            self.add_module(mod)

    def create_subdir(self, *paths, exist_ok=False):
        '''Creates a new directory within the context root and returns the absolute path'''
        if not paths:
            tdir = tempfile.mkdtemp(dir=self._root_dir)
        else:
            tdir = path_join(self._root_dir, *paths)
            makedirs(tdir, exist_ok=exist_ok)
        return tdir

    def build_path(self, *paths):
        '''Returns an absolute path starting from the context root'''
        return path_join(self._root_dir, *paths)

    def add_module(self, module):
        '''Adds a module to the context module set'''
        if isinstance(module, str):
            try:
                module = import_module(module)
            except ImportError:
                raise Exception("Module '{}' was identified as a dependency, but cannot be imported. Ensure that it is installed and available".format(module))
        elif not isinstance(module, ModuleType):
            raise Exception("Module must be of type str or types.ModuleType, not {}".format(type(module)))

        self._modules.add(module)

    @property
    def abspath(self):
        '''Absolute path of the context root directory'''
        return self._root_dir

    @property
    def basename(self):
        '''Base name of the context root directory'''
        return basename(self._root_dir)

    @property
    def modules(self):
        '''The set of all modules (i.e. typing.ModuleType) identified as dependencies'''
        return frozenset(self._modules)

    @property
    def packages(self):
        '''The set of all base packages (i.e. typing.ModuleType with a package) identified as dependencies'''
        return frozenset(mod for mod in self._modules if mod.__package__)

    @property
    def package_names(self):
        '''The set of all base package names identified as dependencies'''
        return frozenset(mod.__name__ for mod in self.packages)

    @property
    def scripts(self):
        '''The set of all scripts (i.e. typing.ModuleType with no package) identified as dependencies'''
        return frozenset(script for script in (mod for mod in self._modules if not mod.__package__) if script.__name__ != '__main__')

    @property
    def script_names(self):
        '''The set of all script names identified as dependencies'''
        return frozenset(mod.__name__ for mod in self.scripts)

    def _load_params(self):
        '''Returns a parameters dict'''
        if isfile(self._params_path):
            with open(self._params_path) as f:
                return json.load(f)
        else:
            return dict()

    def save_params(self):
        '''Saves a parameters json file within the context root'''
        with open(self._params_path, 'w') as f:
            json.dump(self.parameters, f)


@contextlib.contextmanager
def AcumosContextManager(rootdir=None, name=_DEFAULT):
    '''Context manager that provides a AcumosContext object'''
    with _patch_dill():
        if name in _contexts:
            raise Exception("AcumosContext '{}' has already been created. Use `get_context` to access it.".format(name))
        try:
            with _DirManager(rootdir) as rootdir:
                context = AcumosContext(rootdir)
                _contexts[name] = context
                yield context
                context.save_params()
        finally:
            del _contexts[name]


@contextlib.contextmanager
def _DirManager(dir_=None):
    '''Wrapper that passes dir_ through or creates a temporary directory'''
    if dir_ is not None:
        if not isdir(dir_):
            raise Exception("Provided AcumosContext rootdir {} does not exist".format(dir_))
        yield dir_
    else:
        with tempfile.TemporaryDirectory() as tdir:
            yield tdir


def get_context(name=_DEFAULT):
    '''Returns an existing AcumosContext'''
    if name in _contexts:
        return _contexts[name]
    else:
        raise Exception("AcumosContext '{}' has not been created".format(name))
