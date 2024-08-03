netgear-vlan-admin
==================
This is a simple "curses" interface (based on
[Urwid](http://urwid.org/)) to control Netgear smart switches
(and possibly others).

The interface offers control of the VLAN configuration in a more
convenient way (but also more limited to).

This tool supports:

 - Adding, removing and naming VLANs
 - Setting port names
 - Setting port vlan memberships to tagged, untagged or none.
 - Exactly one vlan must be untagged on a port, which also sets the PVID
   implicitlly (this is more limited than switches support, but should
   almost always be sufficient).

This tool was originally written for the FS726T switch, which only has a
webinterface  webinterface that is slow and splits configuration over
different pages making it cumbersome to work with. Later this tool was
extended to support more modern (Netgear) switches as well

This tool was tested on:
 - FS726T
 - GS324G

It likely also works on other (Netgear and maybe other brand) switches
that support SNMP and use the Q-BRIDGE-MIB (RFC 2674) SNMP bindings.

The FS726T backend works by scraping the switch's webinterface, so it is probably
not terribly robust or portable, but it works well enough. The
SNMP backend is more robust and standards-based, so should work better
(but might still make some switch-specific assumptions about what
properties it supports).

![Screenshot](doc/screenshot.png)

Dependencies
------------
This tool requires Python3.8 and a number of python packages (which are
automatically installed by pip/poetry).

In addition, to use SNMP-based switches, some additional system
packages are needed. This was only tested on Linux, should also work on
OSX, probably not on Windows. On Debian:

```
$ sudo apt-get install build-essential libpython3-dev libffi-dev libsmi2-dev snmp-mibs-downloader
```

For other systems, see the [snimpy installation
instructions](https://snimpy.readthedocs.io/en/latest/installation.html).

Installation
------------
Easiest is to install with `pipx` (`apt install pipx`), which creates
a dedicated Python virtualenv and installs the `vlan_admin` and all
dependencies into that, linking the executable into `~/.local/bin` (so
make sure that's in your `$PATH`):

```
pipx install "vlan_admin @ git+https://github.com/matthijskooijman/netgear-vlan-admin.git"
```

To enable SNMP-based switches, add the `snmp` "extra":

```
pipx install "vlan_admin[snmp] @ git+https://github.com/matthijskooijman/netgear-vlan-admin.git"
```

Alternatively, if you have a local clone of the project, you can:

```
pipx install /path/to/netgear-vlan-admin
pipx install /path/to/netgear-vlan-admin[snmp]
```

The same commands above also work with regular `pip` instead of `pipx`,
but then you will have to manually set up a virtualenv, or install into
your global or user-wide Python environments instead (which can be
slightly more messy but is not a problem).

Configuration
-------------
Before starting this tool, create a config file called
`~/.config/vlan-admin.conf`, adding sections for one or more switches.
If you configure multiple switches, you can switch between them
interactively.

For the FS726T switch, use the following section:

```
[name-of-switch]
model = FS726T
address = 192.168.1.1
password = some_password
```

When running the tool. it will add a `[[vlan_names]]` section to store
vlan names (since the switch does not support naming vlans).

For SNMP-based switches using SNMP v2/v2c:

```
[name-of-switch]
model = GS324T
address = 192.168.1.1
community = some_community
```

For SNMP-based switches using SNMPv3:

```
[thuis]
model = GS324T
address = 192.168.1.1
username = some_username

# Enable authentication
auth = SHA
password = auth_password

# Enable encryption
priv = DES
privpassword = encr_password
```

Either authenticatino or encryption can be disabled by omitted the
related config lines.

Supported SNMP switch models are `GS324T` and `GenericNetgearSNMP` (the
latter might support may netgear switches, but this has not been tested
yet).

For the GS324T, the v3 username is always "admin", and the password is
the password for the webui. Also note that a password of more than 15
characters seems to always fail on SNMPv3 on this switch.

Interface
---------
The interface consists of three interactive portions (VLAN/Port
mappings, Port details and VLAN details), which can be cycled through
using the tab key. Use the arrow keys to navigate through the mappings
and use "t", "u" and space to select tagged, untagged and not connected for
each vlan/port combination.

Use F11, or c to commit any pending changes and F10, or q to quit.

When multiple switches are configured, use o to switch between them.

SNMP MIB files
--------------
To allow talking to SNMP-based switches, this tool needs MIB files that
specify the meaning of various SNMP messages. Most of these are based on
publically available RFCs and can be downloaded from various sources,
but most sources are missing some files, have improper filenames, etc.
To make this tool usable out of the box, this repository contains a copy
of the needed MIB files (see the `vlan_admin/snmp-mibs` directory for
details). In the future (at least on Debian-bases sytems if [this bug in
snmp-mibs-downloader](https://bugs.debian.org/1077818) is fixed).

License
-------
This software is licensed under the MIT License:

Copyright (c) 2012-2015 Matthijs Kooijman (matthijs@stdin.nl)

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

In addition, the `vlan_admin/snmp-mibs` directory contains MIB files
with an unclear license, see the README.md file in that directory for
details.
