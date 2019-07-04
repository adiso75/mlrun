# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import uuid
from ast import literal_eval
from os import environ
from tempfile import mktemp

import requests
import yaml

from .kfp import write_kfpmeta
from .execution import MLClientCtx
from .rundb import get_run_db
from .secrets import SecretsStore
from sys import executable, stderr, stdout
from subprocess import run, PIPE


def get_or_create_ctx(name, uid='', event=None, spec=None, with_env=True, rundb=''):

    if event:
        spec = event.body
        uid = uid or event.id

    config = environ.get('MLRUN_EXEC_CONFIG')
    if with_env and config:
        spec = config

    uid = uid or uuid.uuid4().hex
    if spec and not isinstance(spec, dict):
        spec = yaml.safe_load(spec)

    autocommit = False
    tmp = environ.get('MLRUN_META_TMPFILE')
    out = environ.get('MLRUN_META_DBPATH', rundb)
    if out:
        autocommit = True

    ctx = MLClientCtx(name, uid, rundb=out, autocommit=autocommit, tmp=tmp)
    if spec:
        ctx.from_dict(spec)
    return ctx


def run_start(struct, runtime=None, command='', args=[], rundb='', kfp=False, handler=None):

    if handler:
        runtime = HandlerRuntime(handler=handler)
    else:
        if runtime:
            if isinstance(runtime, str):
                runtime = literal_eval(runtime)
            if not isinstance(runtime, dict):
                runtime = runtime.to_dict()

            if 'spec' not in struct.keys():
                struct['spec'] = {}
            struct['spec']['runtime'] = runtime

        if struct and 'spec' in struct.keys() and 'runtime' in struct['spec'].keys():
            kind = struct['spec']['runtime'].get('kind', '')
            if kind in ['', 'local']:
                runtime = LocalRuntime()
            elif kind == 'remote':
                runtime = RemoteRuntime()
            elif kind == 'mpijob':
                runtime = MpiRuntime()
            else:
                raise Exception('unsupported runtime - %s' % kind)

        elif command:
            if '://' in command:
                runtime = RemoteRuntime(command, args)
            else:
                runtime = LocalRuntime(command, args)

        else:
            raise Exception('runtime was not specified via struct or runtime or command!')

    runtime.rundb = rundb
    runtime.process_struct(struct)
    resp = runtime.run()

    # todo: add runtimes, e.g. Horovod, Pipelines workflow

    if not resp:
        return {}
    struct = json.loads(resp)

    if kfp:
        write_kfpmeta(struct)
    return struct


class MLRuntime:
    kind = ''
    def __init__(self, command='', args=[], handler=None):
        self.struct = None
        self.command = command
        self.args = args
        self.handler = handler
        self.rundb = ''

    def process_struct(self, struct):
        self.struct = struct
        if 'spec' not in self.struct.keys():
            self.struct['spec'] = {}
        if 'runtime' not in self.struct['spec'].keys():
            self.struct['spec']['runtime'] = {}

        self.struct['spec']['runtime']['kind'] = self.kind
        if self.command:
            self.struct['spec']['runtime']['command'] = self.command
        else:
            self.command = self.struct['spec']['runtime'].get('command')
        if self.args:
            self.struct['spec']['runtime']['args'] = self.args
        else:
            self.args = self.struct['spec']['runtime'].get('args', [])

    def run(self):
        pass


class LocalRuntime(MLRuntime):

    def run(self):
        environ['MLRUN_EXEC_CONFIG'] = json.dumps(self.struct)
        tmp = mktemp('.json')
        environ['MLRUN_META_TMPFILE'] = tmp
        if self.rundb:
            environ['MLRUN_META_DBPATH'] = self.rundb

        cmd = [executable, self.command]
        if self.args:
            cmd += self.args
        out = run(cmd, stdout=PIPE, stderr=PIPE)
        if out.returncode != 0:
            print(out.stderr.decode('utf-8'), file=stderr)
        print(out.stdout.decode('utf-8'))

        try:
            with open(tmp) as fp:
                resp = fp.read()
            os.remove(tmp)
            return resp
        except FileNotFoundError as err:
            print(err)


class RemoteRuntime(MLRuntime):
    kind = 'remote'
    def run(self):
        secrets = SecretsStore()
        secrets.from_dict(self.struct['spec'])
        self.struct['spec']['secret_sources'] = secrets.to_serial()
        log_level = self.struct['spec'].get('log_level', 'info')
        headers = {'x-nuclio-log-level', log_level}
        try:
            resp = requests.put(self.command, json=json.dumps(self.struct), headers=headers)
        except OSError as err:
            print('ERROR: %s', str(err))
            raise OSError('error: cannot run function at url {}'.format(self.command))

        if not resp.ok:
            print('bad resp!!')
            return None

        logs = resp.headers.get('X-Nuclio-Logs')
        if logs:
            logs = json.loads(logs)
            for line in logs:
                print(line)

        if self.rundb:
            rundb = get_run_db(self.rundb)
            rundb.connect(secrets)
            rundb.store_run(resp.json(), commit=True)

        return resp.json()


class MpiRuntime(MLRuntime):
    kind = 'mpijob'
    def run(self):

        from .mpijob import MpiJob
        uid = self.struct['spec'].get('uid', uuid.uuid4().hex)
        self.struct['spec']['uid'] = uid
        runtime = self.struct['spec']['runtime']

        mpijob = MpiJob.from_dict(runtime.get('spec'))

        mpijob.env('MLRUN_EXEC_CONFIG', json.dumps(self.struct))
        if self.rundb:
            mpijob.env('MLRUN_META_DBPATH', self.rundb)

        mpijob.submit()

        if self.rundb:
            print(uid)

        return None


class HandlerRuntime(MLRuntime):
    kind = 'handler'
    def run(self):
        from nuclio_sdk import Context as _Context, Logger
        from nuclio_sdk.logger import HumanReadableFormatter
        from nuclio_sdk import Event  # noqa

        class FunctionContext(_Context):
            """Wrapper around nuclio_sdk.Context to make automatically create
            logger"""

            def __getattribute__(self, attr):
                value = object.__getattribute__(self, attr)
                if value is None and attr == 'logger':
                    value = self.logger = Logger(level=logging.INFO)
                    value.set_handler(
                        'mlrun', stdout, HumanReadableFormatter())
                return value

            def set_logger_level(self, verbose=False):
                if verbose:
                    level = logging.DEBUG
                else:
                    level = logging.INFO
                value = self.logger = Logger(level=level)
                value.set_handler('mlrun', stdout, HumanReadableFormatter())

        if self.rundb:
            environ['MLRUN_META_DBPATH'] = self.rundb

        out = self.handler(FunctionContext(), Event(body=json.dumps(self.struct)))
        return out



