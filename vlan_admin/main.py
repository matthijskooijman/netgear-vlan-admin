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
import validate

from . import log

from .backends.fs726t import FS726T
from .ui.main import Interface

config_filename = os.path.expanduser("~/.config/vlan-admin.conf")

# This is the structure of the config file. We apply validation to
# make sure all sections are created, even when the config file starts
# out empty.
configspec = """
[vlan_names]
__many__ = string()
"""


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

    # Create a new switch object
    switch = FS726T('192.168.1.253', 'password', config)

    # Create an interface for the switch
    ui = Interface(switch)
    log.ui = ui
    ui.start()

    log.logfile.close()


if __name__ == '__main__':
    main()
