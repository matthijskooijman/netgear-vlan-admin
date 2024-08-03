import pathlib

try:
    import snimpy.manager
    import snimpy.mib
except ImportError as e:
    import sys
    sys.stderr.write(f"Failed to import snimpy: {e}\n")
    sys.stderr.write("Did you install vlan_admin with the 'snmp' extra?\n")
    raise SystemExit

from .common import Port, Switch, Vlan

# Unfortunately these are global, not per-manager instance, so load them here
mib_path = pathlib.Path(__file__).parent.parent / 'snmp-mibs' / 'GS324Tx-v1.0.0.43-Mibs'
snimpy.mib.path(str(mib_path))
snimpy.manager.load('ENTITY-MIB')
snimpy.manager.load('IF-MIB')
snimpy.manager.load('RFC1213-MIB')  # Load after IF-MIB, since IF-MIB adds extra ifOperStatus values
snimpy.manager.load('BRIDGE-MIB')
snimpy.manager.load('Q-BRIDGE-MIB')


class NetgearSnmpSwitch(Switch):
    """
    This class implements controlling netgear switches via SNMP.

    It has been written for the GS324T, but will likely work (maybe with
    some tweaks) for other netgear and maybe other switches as well.

    This uses the following MIBs:
     - ENTITY-MIB (RFC 2737) for general switch info
     - IF-MIB (RFC 1229) and RFC-1213-MIB (RFC 1213) for port status and naming
     - Q-BRIDGE-MIB (RFC 2674) for VLAN configuration
    """

    # (Label, attribute, editable)
    switch_attrs = [
        [
            ('Product', 'product', False),
            ('Software version', 'software_version', False),
            ('MAC address', 'mac_address', False),
            ('Serial number', 'serial_number', False),
        ], [
            ('Hostname', 'hostname', False),
            ('Location', 'location', False),
            ('Contact', 'contact', False),
            ('Uptime', 'uptime', False),
        ]
    ]

    port_attrs = [[
        ('Port number', 'num', False),
        ('Port enabled', 'enabled', False),
        ('Name', 'name', False),
        ('Description', 'description', True),
        ('Link', 'link_status', False),
        ('PVID', 'pvid', False),
    ]]

    vlan_attrs = [[
        ('802.11q VLAN number', 'dotq_id', False),
        ('VLAN name', 'name', True),
    ]]

    def __str__(self):
        return f"{self.product or 'switch'} at {self.address}"

    def __init__(self, config):
        self.address = config["address"]
        community = config.get("community", None)
        username = config.get("username", None)
        password = config.get("password", None)
        auth = config.get("auth", None)
        priv = config.get("priv", None)
        privpassword = config.get("privpassword", None)

        version = int(config.get("version", 3 if username is not None else 2))
        v3_attrs = 'username', 'password', 'auth', 'priv', 'privpassword'

        err = None
        if version == 2:
            if community is None:
                err = f"{config.name}: for snmp version 2, community must be set in config"
            elif any(key in config for key in v3_attrs):
                err = f"{config.name}: for snmp version 2, {','.join(v3_attrs)} must not be set in config"
        elif version == 3:
            if username is None:
                err = f"{config.name}: for snmp version 3, username must be set in config"
            elif (auth is None) != (password is None):
                err = f"{config.name}: for snmp version 3 with authentication, set both password and auth in config",
            elif (priv is None) != (privpassword is None):
                err = f"{config.name}: for snmp version 3 with encryption, set both priv and privpassword in config",
            elif community is not None:
                err = f"{config.name}: for snmp version 3, community must not be set in config"
        else:
            err = f"{config.name}: snmp version can only be 2 or 3"

        if err is not None:
            # TODO: Report error in a better way (but writing to stderr
            # does not work)
            raise ValueError(err)

        self.snmp = snimpy.manager.Manager(
            host=self.address, version=version, community=community,
            secname=username, authpassword=password, authprotocol=auth,
            privprotocol=priv, privpassword=privpassword,
        )

        super().__init__(config)

    def commit_port_description_change(self, port, description):
        self._emit('status_changed', f"Committing port {port.num} description...")
        self.snmp.ifAlias[port.if_index] = description

    def commit_vlan_description_change(self, vlan, description):
        self._emit('status_changed', f"Committing vlan {vlan.dotq_id} description...")
        self.snmp.dot1qVlanStaticName[vlan.dotq_id] = description

    def commit_vlan_memberships(self, vlan, memberships):
        self._emit('status_changed', f"Committing vlan {vlan.dotq_id} memberships...")

        egress = bytearray(b'\x00' * ((len(self.ports) + 7) // 8))
        untagged = bytearray(b'\x00' * ((len(self.ports) + 7) // 8))

        def set_port_bit(bstr, port_num):
            # Lowest port number (1) maps to first byte, MSB
            bit = port.num - 1
            bstr[bit // 8] |= 0x80 >> (bit % 8)

        for port, value in zip(self.ports, memberships):
            if value == Vlan.TAGGED:
                set_port_bit(egress, port)
            elif value == Vlan.UNTAGGED:
                set_port_bit(egress, port)
                set_port_bit(untagged, port)

        self.snmp.dot1qVlanStaticEgressPorts[vlan.dotq_id] = egress
        self.snmp.dot1qVlanStaticUntaggedPorts[vlan.dotq_id] = untagged

    def commit_pvids(self, pvids):
        self._emit('status_changed', "Committing PVID settings...")
        for port, pvid in zip(self.ports, pvids):
            self.snmp.dot1qPvid[port.num] = pvid

    def commit_vlan_delete(self, vlan):
        self._emit('status_changed', f"Deleting vlan {vlan.dotq_id}...")
        self.snmp.dot1qVlanStaticRowStatus[vlan.dotq_id] = "destroy"

    def commit_vlan_add(self, vlan):
        self._emit('status_changed', f"Creating vlan {vlan.dotq_id}...")
        self.snmp.dot1qVlanStaticRowStatus[vlan.dotq_id] = "createAndGo"

    def get_status(self):
        self._emit('status_changed', "Retrieving switch status...")
        self.hostname = self.snmp.sysName.decode()
        self.location = self.snmp.sysLocation.decode()
        self.contact = self.snmp.sysContact.decode()
        self.uptime = self.snmp.sysUpTime

        mac = self.snmp.dot1dBaseBridgeAddress
        raw_len = 6
        encoded_len = raw_len * 2 + raw_len - 1
        double_encoded_len = encoded_len * 2 + encoded_len - 1

        if isinstance(mac, bytes) and len(mac) == raw_len:
            # Compliant device returning 6 raw bytes with no MIB-based processing
            self.mac_address = mac.hex(':')
        elif isinstance(mac, bytes) and len(mac) == encoded_len:
            # Non-compliant device returning MAC address as hex string
            # with colons, with no MIB-based processing
            self.mac_address = mac.decode()
        elif isinstance(mac, str) and len(mac) == encoded_len:
            # Compliant device with MIB decoding based on DISPLAY-HINT "1x:"
            self.mac_address = mac
        elif isinstance(mac, str) and len(mac) == double_encoded_len:
            # Non-compliant device return MAC address as hex string,
            # with MIB decoding based on DISPLAY-HINT "1x:". Reverse one
            # layer of hex conversion.
            self.mac_address = bytes.fromhex(mac.replace(':', '')).decode()
        else:
            raise ValueError(f"Unsupported MAC address encoding: {mac}")

        # On all tested netgear switches, the "chassis" entity contains the
        # useful info, so look for the first chassis entity.
        for i, cls in self.snmp.entPhysicalClass.iteritems():
                if cls == "chassis":
                    self.product = self.snmp.entPhysicalModelName[i]
                    self.software_version = self.snmp.entPhysicalSoftwareRev[i]
                    self.serial_number = self.snmp.entPhysicalSerialNum[i]
                    break

        # Prefetch these values for all ports at once, which is a *lot*
        # faster than fetching them one by one in the below loop. Would
        # be easier if we could just fetch the entire table, but this is
        # not supported yet: https://github.com/vincentbernat/snimpy/issues/46#issuecomment-209917027
        all_names = dict(self.snmp.ifName.iteritems())
        all_descriptions = dict(self.snmp.ifAlias.iteritems())
        all_speeds = dict(self.snmp.ifHighSpeed.iteritems())
        all_admin_statuses = dict(self.snmp.ifAdminStatus.iteritems())
        all_oper_statuses = dict(self.snmp.ifOperStatus.iteritems())
        all_pvids = dict(self.snmp.dot1qPvid)

        for bridge_port, if_index in self.snmp.dot1dBasePortIfIndex.iteritems():
            name = all_names[if_index]
            description = all_descriptions[if_index]
            speed = all_speeds[if_index]
            admin_status = all_admin_statuses[if_index]
            oper_status = all_oper_statuses[if_index]
            pvid = all_pvids[bridge_port]

            if admin_status == "up":
                enabled = True
            elif admin_status == "down":
                enabled = False
            else:
                enabled = None

            if oper_status == "notPresent":
                # This is used for LAGs that are not configured or
                # (weirdly - on GS324T) have all their ports down. In
                # the latter case, we also cannot retrieve (and
                # presumably modify) the vlan config for these LAGs, so
                # just ignore these ports
                continue

            if oper_status == "up" and speed:
                link_status = f"{speed}M" if speed else "Down"
            elif oper_status == "up":
                link_status = "Up"
            elif oper_status == "down":
                link_status = "Down"
            else:
                link_status = f"Other: {oper_status}"

            port = Port(
                self, bridge_port, link_status=link_status,
                description=description, name=name, if_index=if_index,
                enabled=enabled, pvid=pvid,
            )
            self.ports.append(port)

        # Again, prefetch for efficiency
        all_egress = dict(self.snmp.dot1qVlanStaticEgressPorts.iteritems())
        all_untagged = dict(self.snmp.dot1qVlanStaticUntaggedPorts.iteritems())

        for vlan_id, name in self.snmp.dot1qVlanStaticName.iteritems():

            def get_port_bit(bstr, port):
                # Lowest port number (1) maps to first byte, MSB
                bit = port.num - 1
                return bstr[bit // 8] >> (7 - (bit % 8)) & 1

            vlan = Vlan(self, internal_id=vlan_id, dotq_id=vlan_id, name=name)
            self.vlans.append(vlan)
            self.dotq_vlans[vlan_id] = vlan

            for port in self.ports:
                if get_port_bit(all_egress[vlan_id], port):
                    if get_port_bit(all_untagged[vlan_id], port):
                        vlan.ports[port] = Vlan.UNTAGGED
                    else:
                        vlan.ports[port] = Vlan.TAGGED
                else:
                    vlan.ports[port] = Vlan.NOTMEMBER

        self._emit('details_changed')
        self._emit('portlist_changed')
        self._emit('status_changed', None)
