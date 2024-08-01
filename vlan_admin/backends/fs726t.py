import bs4
import urllib.error
import urllib.parse
import urllib.request
import re
import sys

from ..log import log
from .common import Port, Switch, Vlan


class LoginException(Exception):
    pass


class FS726T(Switch):
    # (Label, attribute, editable)
    switch_attrs = [
        [
            ('Product', 'product', False),
            ('Firmware version', 'firmware_version', False),
            ('Protocol version', 'protocol_version', False),
            ('MAC address', 'mac_address', False),
        ], [
            ('IP configuration', 'ip_config', False),
            ('IP address', 'ip_address', False),
            ('IP netmask', 'ip_netmask', False),
            ('IP gateway', 'ip_gateway', False),
        ], [
            ('Hostname', 'hostname', False),
            ('Location', 'location', False),
            ('Login timeout', 'login_timeout', False),
            ('Uptime', 'uptime', False),
        ]
    ]

    port_attrs = [[
        ('Port number', 'num', False),
        ('Description', 'description', True),
        ('Port speed', 'speed', False),
        ('Configured speed', 'speed_setting', False),
        ('Flow control', 'flow_control', False),
        ('Link', 'link_status', False),
        ('PVID', 'pvid', False),
    ]]

    vlan_attrs = [[
        ('802.11q VLAN number', 'dotq_id', False),
        ('VLAN name', 'name', True),
    ]]

    def __init__(self, address=None, password=None, config=None):
        self.address = address
        self.password = password
        self.max_vlan_internal_id = 0

        super().__init__(config)

    def __str__(self):
        return f"{self.__class__.__name__} at {self.address}"

    def request(self, path, data=None, status=None, auto_login=True):
        if status:
            self._emit('status_changed', status)

        url = "http://%s%s" % (self.address, path)

        if data and not isinstance(data, str):
            data = urllib.parse.urlencode(data).encode()

        if data is not None:
            log("HTTP POST request to %s (POST data %s)" % (url, str(data)))
        else:
            log("HTTP GET request to %s" % url)

        response = urllib.request.urlopen(url, data).read().decode()
        log('Done')

        if auto_login and "<input type=submit value=' Login '>" in response:
            self.do_login()
            return self.request(path, data, status)
        if "Only one user can login" in response:
            raise LoginException("Can only login from a single IP address: Log out the other client first")
        return response

    def do_login(self):
        """
        Log into this device. Be sure to always call do_logout as well,
        to prevent other users from getting locked out.
        """

        # Apparently, login is done on an IP basis, not tracked by cookies.
        # Do not change the order of parameters, that breaks the request :-S
        data = [
            ('passwd', self.password),
            ('post_url', "/cgi/device"),
        ]
        html = self.request("/cgi/device", data, "Logging in...")

        # Succesful login
        if '<h1>Switch Status</h1>' in html:
            return

        # See if we can find an error message
        error = re.search(
            "<font color=#336699 size=3><br><b>(.*)<br><br><input type=submit value='Continue'><br><br>",
            html,
        )
        if (error):
            raise LoginException("Error occured at login. Device said: %s" % error.group(1))

        # Other error?
        raise LoginException("Login seems to have failed, but no error message could be found")

    def do_logout(self):
        """
        Log out. This is important, since the device does not allow parallel
        sessions, so not logging out means you'll have to wait for the
        session timeout before you can log in again.
        """
        try:
            self.request("/cgi/logout", status="Logging out...", auto_login=False)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                sys.stderr.write("Ignoring logout error, we're probably not logged in.\n")
            else:
                raise

    def commit_all(self):
        super().commit_all()

        # Renumber the remaining vlans (just like the switch does
        # internally).
        for i in range(0, len(self.vlans)):
            self.vlans[i].internal_id = i + 1
        self.max_vlan_internal_id = len(self.vlans)

    def commit_port_description_change(self, port, name):
        # Do not change the order of parameters, that breaks the request :-S
        data = [
            ('portset', port.num - 1),  # "portset" numbers from 0
            ('port_des', name),
            ('post_url', '/cgi/portdetail'),
        ]

        self.request("/cgi/portdetail=%s" % (port.num - 1), data, "Committing port %d description..." % (port.num))

    def commit_vlan_description_change(self, vlan, description):
        self.config['vlan_names']['vlan%d' % vlan.dotq_id] = description
        self.config.write()

    def commit_vlan_memberships(self, vlan, memberships):
        status = "Committing vlan %d memberships..." % (vlan.dotq_id)
        # If no internal id is present yet, assign the next one. Calling
        # setvid with a non-existing tag_id will make the switch
        # conveniently create the vlan.
        if vlan.internal_id is None:
            self.max_vlan_internal_id += 1
            vlan.internal_id = self.max_vlan_internal_id
            status = "Creating vlan %d..." % (vlan.dotq_id)

        # Do not change the order of parameters, that breaks the request :-S
        # Note that in the webgui, the 'vid' parameter is only present
        # in the add new vlan request, but doesn't seem to break the
        # update vlan request either, so we just add it unconditionally.
        data = [
            ('tag_id', vlan.internal_id),
            ('vid', vlan.dotq_id),
            ('post_url', '/cgi/setvid'),
            ('vid_mem', ','.join([str(m) for m in memberships])),
        ]

        self.request("/cgi/setvid=%s" % (vlan.internal_id), data, status)

    def commit_pvids(self, pvids):
        # Do not change the order of parameters, that breaks the request :-S
        data = [
            ('tag_id', 255),
        ] + [
            ('dvid', pvid) for pvid in pvids
        ] + [
            ('post_url', '/cgi/pvid'),
        ]

        self.request("/cgi/pvid", data, "Committing PVID settings..")

    def commit_vlan_delete(self, vlan):
        # Do not change the order of parameters, that breaks the request :-S
        data = [
            ('tag_id', vlan.internal_id),
            ('del_tag', 'on'),
            ('post_url', '/cgi/delvid'),
            ('vid_mem', ''),
        ]
        self.request("/cgi/setvid=%s" % (vlan.internal_id), data, "Deleting vlan %d..." % (vlan.dotq_id))

        # Remove the name from the config
        self.config['vlan_names'].pop('vlan%d' % vlan.dotq_id, None)
        self.config.write()

    def get_status(self):
        soup = bs4.BeautifulSoup(
            self.request("/cgi/device", status="Retrieving switch status..."),
            # Force using the lxml parser, since the builtin python
            # parser does not handle the incorrect HTML produced by the
            # switch correctly...
            'lxml',
        )

        try:
            self.parse_status(soup)
            self._emit('details_changed')
            self._emit('portlist_changed')
        except AttributeError:
            # Print HTML for debugging
            print(soup)
            raise

        self._emit('status_changed', None)

    def parse_status(self, soup):
        #####################################
        # Parse switch information
        #####################################
        h1 = soup.find(string="Switch Status")
        h1_table = h1.findParent('table')
        table = h1_table.findNext("table")

        rows = table.findAll('tr')

        for row in rows:
            tds = row.findAll('td')
            key = self.remove_html_tags(tds[0].text.strip())
            value = self.remove_html_tags(tds[1].text.strip())
            if key == 'Product Name':
                self.product = value
            elif key == 'Firmware Version':
                self.firmware_version = value
            elif key == 'Protocol Version':
                self.protocol_version = value
            elif key == 'DHCP' and value == 'Disable':
                self.ip_config = 'Static'
            elif key == 'DHCP' and value == 'Enable':
                self.ip_config = 'DHCP'
            elif key == 'IP address':
                self.ip_address = value
            elif key == 'Subnet mask':
                self.ip_netmask = value
            elif key == 'Default gateway':
                self.ip_gateway = value
            elif key == 'MAC address':
                self.mac_address = value
            elif key == 'System Name':
                self.hostname = value
            elif key == 'Location Name':
                self.location = value
            elif key == 'Login Timeout (minutes)':
                self.login_timeout = value + ' minutes'
            elif key == 'System UpTime':
                self.uptime = value
            else:
                sys.stderr.write('Ignoring unknown table row: %s = %s\n' % (key, value))

        #####################################
        # Parse PVID information
        #####################################
        h1 = soup.find(string="IEEE 802.1Q PVID Table").parent
        table = h1.findNext("table")

        rows = table.findAll('tr')

        # The top row is the header (but nobody bothered putting it
        # in a thead, of course).
        pvid_rows = rows[1:]

        pvids = {}

        for row in pvid_rows:
            tds = row.findAll('td')
            if (tds):
                # Each row contains info for four ports, so iterate them
                for port_tds in (tds[0:2], tds[2:4], tds[4:6], tds[6:8]):
                    (num, pvid) = [td.text.strip() for td in port_tds]
                    if num:
                        pvids[int(num)] = int(pvid)

        #####################################
        # Parse port information
        #####################################
        h1 = soup.find(string="PORT Status").parent
        table = h1.findNext("table")

        rows = table.findAll('tr')

        # The top two rows are the header (but nobody bothered putting them
        # in a thead, of course). Note that the second header row is
        # empty...
        port_rows = rows[1:]

        speed = "unknown"
        self.ports = []
        for row in port_rows:
            # There are rows containing a single th tag that specify the
            # speed for the subsequent ports.
            ths = row.findAll('th')
            if (ths and len(ths) == 1):
                speed = ths[0].text.strip()
                continue

            tds = row.findAll('td')
            if (tds):
                # Each row contains info for two ports, so iterate them
                for port_tds in (tds[0:5], tds[5:10]):
                    (num, speed_setting, flow_control, link_status, description) = [td.text.strip() for td in port_tds]
                    assert len(self.ports) == int(num) - 1, "Switch ports are not numbers consecutively?"
                    pvid = pvids[int(num)]

                    port = Port(
                        self, int(num),
                        speed=speed,
                        speed_setting=speed_setting,
                        flow_control=flow_control,
                        link_status=link_status,
                        description=description,
                        pvid=pvid,
                    )
                    self.ports.append(port)

        #####################################
        # Parse vlan information
        #####################################
        h1 = soup.find(string="IEEE 802.1Q VLAN Settings").parent
        table = h1.findNext("table")

        rows = table.findAll('tr')

        # The top two rows are the header (but nobody bothered putting them
        # in a thead, of course)
        vlan_rows = rows[2:]

        self.vlans = []
        self.dotq_vlans = {}
        for row in vlan_rows:
            tds = row.findAll('td')
            # We assume the vlans are listed in order of their internal
            # id (e.g., order of creation, one-based)
            internal_id = len(self.vlans) + 1
            # The first td shows the 802.11q id
            dotq_id = int(tds[0].text.strip())

            # Create the vlan descriptor
            name = self.config['vlan_names'].get('vlan%d' % dotq_id, '')
            vlan = Vlan(self, internal_id, dotq_id, name)
            self.vlans.append(vlan)
            self.dotq_vlans[dotq_id] = vlan

            assert len(tds) == len(self.ports) + 1, "VLAN table has wrong number of ports?"
            for port in self.ports:
                # We skip td[0] (which contains a header), since we use
                # the 1-based port number
                text = tds[port.num].text.strip()
                if text == '':
                    vlan.ports[port] = Vlan.NOTMEMBER
                elif text == 'T':
                    vlan.ports[port] = Vlan.TAGGED
                elif text == 'U':
                    vlan.ports[port] = Vlan.UNTAGGED
                else:
                    sys.stderr.write('Ignoring unknown vlan/port status: %s \n' % text)

        self.max_vlan_internal_id = len(self.vlans)

    def remove_html_tags(self, data):
        p = re.compile(r'<.*?>')
        return p.sub('', data)
