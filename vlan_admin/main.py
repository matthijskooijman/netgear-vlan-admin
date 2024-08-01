#!/usr/bin/python

# This software is licensed under the MIT License:
#
# Copyright (c) 2012-2015 Matthijs Kooijman <matthijs@stdin.nl>
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


import io
import os.path
import configobj
import importlib
import sys
import validate

from . import log

from .ui.main import Interface

config_filename = os.path.expanduser("~/.config/vlan-admin.conf")

# This is the structure of the config file. We apply validation to
# make sure all sections are created, even when the config file starts
# out empty.
# Validation should really be model-specific, but that might not be
# supported by configobj
configspec = """
[__many__]
    model = string()
    address = string()
    password = string() # fs726t only

[[vlan_names]] # fs726t only
__many__ = string()
"""

models = {
    'FS726T': ('.backends.fs726t', 'FS726T'),
}


def switch_constructor(section):
    try:
        model_name = section['model']
    except KeyError:
        sys.stderr.write(f"No model specified in config for section: {section.name}\n")
        raise SystemExit

    try:
        module_name, class_name = models[model_name]
    except KeyError:
        sys.stderr.write(f"Invalid model in config section {section.name}: {section.backend}\n")
        sys.stderr.write(f"Supported models are: {','.join(models.keys())}\n")
        raise SystemExit

    module = importlib.import_module(module_name, __package__)
    constructor = getattr(module, class_name)

    def create():
        return constructor(section)
    return create


def main():
    log.logfile = open('vlan-admin.log', 'a')

    # Create the switch object
    config = configobj.ConfigObj(
        infile=config_filename,
        configspec=io.StringIO(configspec),
        create_empty=True,
        encoding='UTF8',
    )
    config.validate(validate.Validator())

    switches = {}
    for name, section in config.items():
        switches[section.name] = switch_constructor(section)

    if not switches:
        sys.stderr.write(f"No switches configured in config file ({config_filename})\n")
        return

    # Create an interface for the switch
    ui = Interface(switches)
    log.ui = ui
    ui.start()

    log.logfile.close()


if __name__ == '__main__':
    main()
