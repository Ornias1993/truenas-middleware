from middlewared.service import Service


class DetectFailoverStatusService(Service):

    class Config:
        private = True
        namespace = 'failover.status'

    async def get_local(self, app):

        # Check if we have at least 1 interface with VRRP config
        interfaces = await self.middleware.call('interface.query')
        if not any(filter(lambda x: x['state']['vrrp_config'], interfaces)):
            return 'SINGLE'

        # Check if we have at least 1 pool configured and imported
        pools = await self.middleware.call('pool.query')
        if not pools:
            return 'SINGLE'

        # Check if we have any VRRP MASTER interfaces
        if (await self.middleware.call('failover.vip.get_states', interfaces))[0]:

            # If we have VRRP MASTER interfaces, ensure none of the zpools
            # are offline
            if any(filter(lambda x: x.get('status') != 'OFFLINE', pools)):
                return 'MASTER'

            # check if there is an ongoing failover event
            failover_events = await self.middleware.call(
                'core.get_jobs', [
                    ('OR', [
                        ('method', '=', 'failover.event.vrrp_master'),
                        ('method', '=', 'failover.event.vrrp_backup'),
                    ])
                ]
            )

            # only care about RUNNING events
            for i in failover_events:
                if i['state'] == 'RUNNING':
                    return i['progress']['description']
