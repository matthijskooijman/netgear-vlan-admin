# Netgear SNMP MIB files
This directory contains the relevant MIB files for talking to the GS324T
switch (and probably other switches too). In essence, these MIB files
are just extracts from the relevant RFCs and nothing netgear-specific.

On Debian-based systems, most of these MIB files can be installed using
the snmp-mibs-downloader package, but that misses a few files that are
needed by this tool, so until those are added (see [this bug
report](https://bugs.debian.org/1077818)), this tool uses its own copy
of these MIB files (some of which are a bit older than the ones from
snmp-mibs-downloader, because of superseded RFCs).

These files are added verbatim (with renaming, see below) from the
[GS324Tx-v1.0.0.43-Mibs.zip](https://www.downloads.netgear.com/files/GDC/GS324T/GS324Tx-v1.0.0.43-Mibs.zip)
downloaded via the [Netgear GS324T support page](https://www.netgear.com/support/product/gs324t)
(under "Firmware and software downloads").

Note that Netgear does not define any license on these files, and Debian
goes out of their way to do dynamic extraction of MIB files from rfc
files, presumably because the license for (older) RFCs does not allow
redistribution of content extracted from the RFC. This likely means that
this directory technically violates licensing conditions, but that seems
an acceptable risk for the convenience offered.

The files in this directory were renamed as follows:

    mib-2.my => RFC1213-MIB
    rfc1212.my => RFC-1212
    smi.my => RFC1155-SMI
    entity.my => ENTITY-MIB
    v3-arch.my => SNMP-FRAMEWORK-MIB
    v2-smi.my => SNMPv2-SMI
    v2-tc.my => SNMPv2-TC
    v2-mib.my => SNMPv2-MIB
    v2-conf.my => SNMPv2-CONF
    bridge.my => BRIDGE-MIB
    pbridge.my => P-BRIDGE-MIB
    vlan.my => Q-BRIDGE-MIB
    rfc1215.my => RFC-1215
    if.my => IF-MIB
    iftype.my => IANAifType-MIB

Note that this does not contain all the MIB files from the netgear zip
file, only the ones required by this tool.
