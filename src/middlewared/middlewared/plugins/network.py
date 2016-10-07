from middlewared.service import Service, private

import gevent
import ipaddress
import netif
import os
import re
import signal
import subprocess


class InterfacesService(Service):

    def sync(self):
        """
        Sync interfaces configured in database to the OS.
        """

        # First of all we need to create the virtual interfaces
        # LAGG comes first and then VLAN
        laggs = self.middleware.call('datastore.query', 'network.lagginterface')
        for lagg in laggs:
            name = lagg['lagg_interface']['int_name']
            try:
                iface = netif.get_interface(name)
            except KeyError:
                netif.create_interface(name)
                iface = netif.get_interface(name)

            if lagg['lagg_protocol'] == 'fec':
                iface.protocol = netif.AggregationProtocol.ETHERCHANNEL
            else:
                iface.protocol = getattr(netif.AggregationProtocol, lagg['lagg_protocol'].upper())

            members_configured = set(p[0] for p in iface.ports)
            members_database = set()
            for member in self.middleware.call('datastore.query', 'network.lagginterfacemembers', [('lagg_interfacegroup_id', '=', lagg['id'])]):
                members_database.add(member['lagg_physnic'])

            # Remeve member configured but not in database
            for member in (members_configured - members_database):
                iface.delete_port(member)

            # Add member in database but not configured
            for member in (members_database - members_configured):
                iface.add_port(member)

        vlans = self.middleware.call('datastore.query', 'network.vlan')
        for vlan in vlans:
            try:
                iface = netif.get_interface(vlan['vlan_vint'])
            except KeyError:
                netif.create_interface(vlan['vlan_vint'])
                iface = netif.get_interface(vlan['vlan_vint'])

            if iface.parent != vlan['vlan_pint'] or iface.tag != vlan['vlan_tag']:
                iface.unconfigure()
                iface.configure(vlan['vlan_pint'], vlan['vlan_tag'])

        interfaces = [i['int_interface'] for i in self.middleware.call('datastore.query', 'network.interfaces')]
        for interface in interfaces:
            self.sync_interface(interface)


        internal_interfaces = ['lo', 'pflog', 'pfsync', 'tun', 'tap']
        if not self.middleware.call('system.is_freenas'):
            internal_interfaces.extend(self.middleware.call('notifier.failover_internal_interfaces') or [])
        internal_interfaces = tuple(internal_interfaces)

        # Destroy interfaces which are not in database
        for name, iface in list(netif.list_interfaces().items()):
            if name.startswith(internal_interfaces)
                continue
            elif name.startswith(('lagg', 'vlan')):
                if name not in interfaces:
                    netif.destroy_interface(name)
            else:
                if name not in interfaces:
                    # Physical interface not in database lose addresses
                    for address in iface.addresses:
                        iface.remove_address(address)

                    # Kill dhclient if its running for this interface
                    dhclient_running, dhclient_pid = self.dhclient_status(name)
                    if dhclient_running:
                        os.kill(dhclient_pid, signal.SIGTERM)

    @private
    def alias_to_addr(self, alias):
        addr = netif.InterfaceAddress()
        ip = ipaddress.ip_interface(u'{}/{}'.format(alias['address'], alias['netmask']))
        addr.af = getattr(netif.AddressFamily, 'INET6' if ':' in alias['address'] else 'INET')
        addr.address = ip.ip
        addr.netmask = ip.netmask
        addr.broadcast = ip.network.broadcast_address
        if 'vhid' in alias:
            addr.vhid = alias['vhid']
        return addr

    @private
    def sync_interface(self, name):
        data = self.middleware.call('datastore.query', 'network.interfaces', [('int_interface', '=', name)], {'get': True})
        aliases = self.middleware.call('datastore.query', 'network.alias', [('alias_interface_id', '=', data['id'])])

        iface = netif.get_interface(name)

        addrs_database = set()
        addrs_configured = set([
            a for a in iface.addresses
            if a.af != netif.AddressFamily.LINK
        ])

        has_ipv6 = data['int_ipv6auto'] or False

        dhclient_running, dhclient_pid = self.dhclient_status(name)
        if dhclient_running and data['int_dhcp']:
            dhclient_leasesfile = '/var/db/dhclient.leases.{}'.format(name)
            if os.path.exists(dhclient_leasesfile):
                with open(dhclient_leasesfile, 'r') as f:
                    dhclient_leases = f.read()
                reg_address = re.search(r'fixed-address\s+(.+);', dhclient_leases)
                reg_netmask = re.search(r'option subnet-mask\s+(.+);', dhclient_leases)
                if reg_address and reg_netmask:
                    addrs_database.add(self.alias_to_addr({
                        'address': reg_address.group(1),
                        'netmask': reg_netmask.group(1),
                    }))
                else:
                    self.logger.info('Unable to get address from dhclient')
        else:
            if data['int_ipv4address']:
                addrs_database.add(self.alias_to_addr({
                    'address': data['int_ipv4address'],
                    'netmask': data['int_v4netmaskbit'],
                }))
            if data['int_ipv6address']:
                addrs_database.add(self.alias_to_addr({
                    'address': data['int_ipv6address'],
                    'netmask': data['int_v6netmaskbit'],
                }))
                has_ipv6 = True

        carp_vhid = carp_pass = None
        if data['int_vip']:
            addrs_database.add(self.alias_to_addr({
                'address': data['int_vip'],
                'netmask': '32',
                'vhid': data['int_vhid'],
            }))
            carp_vhid = data['int_vhid']
            carp_pass = data['int_pass'] or None

        for alias in aliases:
            if alias['alias_v4address']:
                addrs_database.add(self.alias_to_addr({
                    'address': alias['alias_v4address'],
                    'netmask': alias['alias_v4netmaskbit'],
                }))
            if alias['alias_v6address']:
                addrs_database.add(self.alias_to_addr({
                    'address': alias['alias_v6address'],
                    'netmask': alias['alias_v6netmaskbit'],
                }))
                has_ipv6 = True

            if alias['alias_vip']:
                addrs_database.add(self.alias_to_addr({
                    'address': alias['alias_vip'],
                    'netmask': '32',
                    'vhid': data['int_vhid'],
                }))

        if carp_vhid:
            iface.carp_config = netif.CarpConfig(carp_vhid, None, key=carp_pass)

        if has_ipv6:
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.IFDISABLED}
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.AUTO_LINKLOCAL}
        else:
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.IFDISABLED}
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.AUTO_LINKLOCAL}

        # Remove addresses configured and not in database
        for addr in (addrs_configured - addrs_database):
            if (
                addr.af == netif.AddressFamily.INET6 and
                str(addr.address).startswith('fe80::')
            ):
                continue
            iface.remove_address(addr)

        # Add addresses in database and not configured
        for addr in (addrs_database - addrs_configured):
            iface.add_address(addr)

        # If dhclient is not running and dhcp is configured, lets start it
        if not dhclient_running and data['int_dhcp']:
            gevent.spawn(self.dhclient_start, data['int_interface'])
        elif dhclient_running and not data['int_dhcp']:
            os.kill(dhclient_pid, signal.SIGTERM)

        if data['int_ipv6auto']:
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.ACCEPT_RTADV}
            subprocess.Popen(
                ['/etc/rc.d/rtsold', 'onestart'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            ).wait()
        else:
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.ACCEPT_RTADV}

    @private
    def dhclient_status(self, interface):
        pidfile = '/var/run/dhclient.{}.pid'.format(interface)
        pid = None
        if os.path.exists(pidfile):
            with open(pidfile, 'r') as f:
                try:
                    pid = int(f.read().strip())
                except ValueError:
                    pass

        running = False
        if pid:
            try:
                os.kill(pid, 0)
            except OSError:
                pass
            else:
                running = True
        return running, pid

    @private
    def dhclient_start(self, interface):
        proc = subprocess.Popen([
            '/sbin/dhclient', '-b', interface,
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
        output = proc.communicate()[0]
        if proc.returncode != 0:
            self.logger.error('Failed to run dhclient on {}: {}'.format(
                interface, output,
            ))
