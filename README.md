netgear-vlan-admin
==================
This is a simple "curses" interface (based on
[Urwid](http://urwid.org/)) to control Netgear FS726T smart switches
(and possibly others).

The interface offers control of the VLAN configuration in a more
convenient way, since the switch webinterface is slow and splits
configuration over different pages.

This tool works by scraping the switch's webinterface, so it is probably
not terribly robust or portable, but it works well enough.

Configuration
-------------
To configure the switch's IP address and password, modify vlan-admin.py.
Look for this line:

        switch = FS726T('192.168.1.253', 'password', config)

Additionally, the tool stores vlan names in `~/.config/vlan-admin.conf`,
since the switch itself doesn't support naming vlans.

Interface
---------
The interface consists of three interactive portions (VLAN/Port
mappings, Port details and VLAN details), which can be cycled through
using the tab key. Use the arrow keys to navigate through the mappings
and use "t", "u" and space to select tagged, untagged and not connected for
each vlan/port combination.

Use F11 to commit any pending changes and F10, or q to quit.
