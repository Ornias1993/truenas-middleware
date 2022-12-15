import aiodocker
import contextlib
import itertools

from copy import deepcopy
from datetime import datetime

from middlewared.schema import Bool, Datetime, Dict, Int, List, returns, Str
from middlewared.service import accepts, CallError, filterable, job, private, CRUDService
from middlewared.utils import filter_list

from .client import ContainerdClient
from .utils import DEFAULT_DOCKER_REGISTRY, DEFAULT_DOCKER_REPO


class ContainerImagesService(CRUDService):

    class Config:
        datastore_primary_key_type = 'string'
        namespace = 'container.image'
        namespace_alias = 'docker.images'
        cli_namespace = 'app.docker.image'

    ENTRY = Dict(
        'container_image_entry',
        Str('id'),
        Dict('labels', additional_attrs=True),
        List('repo_tags', items=[Str('repo_tag')]),
        List('repo_digests', items=[Str('repo_digest')]),
        Int('size'),
        Bool('dangling'),
        Bool('update_available'),
        Bool('system_image'),
        Datetime('created', null=True),
        List('parsed_repo_tags', items=[Dict(
            'parsed_repo_tag',
            Str('image'),
            Str('tag'),
            Str('registry'),
            Str('complete_tag'),
        )]),
        List('complete_tags', items=[Str('complete_tag')]),
    )

    @filterable
    def query(self, filters, options):
        """
        Retrieve container images present in the system.

        `query-options.extra.parse_tags` is a boolean which when set will have normalized tags to be retrieved
        for container images.
        """
        results = []
        if not self.middleware.call_sync('service.started', 'docker'):
            return results

        extra = deepcopy(options.get('extra', {}))
        update_cache = self.middleware.call_sync('container.image.image_update_cache')
        system_images = self.middleware.call_sync('container.image.get_system_images_tags')
        parse_tags = extra.get('parse_tags', False) or extra.get('complete_tags', False)

        with ContainerdClient('image') as client:
            for image in client.list_images():
                repo_tags = image['repoTags'] or []
                system_image = any(tag in system_images for tag in repo_tags)
                created_at = None
                with contextlib.suppress(ValueError, KeyError):
                    # We have seen cases where docker returns N/A for created so let's handle this safely
                    created_at = datetime.fromtimestamp(int(image['Created']))

                result = {
                    'id': image['Id'],
                    'labels': {},  # TODO: We are not getting these so far
                    'repo_tags': repo_tags,
                    'repo_digests': image.get('repoDigests') or [],
                    'size': image['size'],
                    'created': created_at,
                    'dangling': len(repo_tags) == 1 and repo_tags[0] == '<none>:<none>',
                    'update_available': not system_image and any(update_cache[r] for r in repo_tags),
                    'system_image': system_image,
                }
                if parse_tags:
                    result['parsed_repo_tags'] = self.middleware.call_sync('container.image.parse_tags', repo_tags)
                if extra.get('complete_tags', False):
                    result['complete_tags'] = [tag['complete_tag'] for tag in result['parsed_repo_tags']]

                results.append(result)
        return filter_list(results, filters, options)

    @accepts(
        Dict(
            'image_pull',
            Dict(
                'docker_authentication',
                Str('username', required=True),
                Str('password', required=True),
                default=None,
                null=True,
            ),
            Str('from_image', required=True),
            Str('tag', default=None, null=True),
        )
    )
    @returns(List(items=[Dict('pull_result_entry', Str('status'), additional_attrs=True)]))
    @job()
    async def pull(self, job, data):
        """
        `from_image` is the name of the image to pull. Format for the name is "registry/repo/image" where
        registry may be omitted and it will default to docker registry in this case.

        `tag` specifies tag of the image and defaults to `null`. In case of `null` it will retrieve all the tags
        of the image.

        `docker_authentication` should be specified if image to be retrieved is under a private repository.
        """
        await self.docker_checks()
        # TODO: Have job progress report downloading progress
        async with aiodocker.Docker() as docker:
            try:
                response = await docker.images.pull(
                    from_image=data['from_image'], tag=data['tag'], auth=data['docker_authentication']
                )
            except aiodocker.DockerError as e:
                raise CallError(f'Failed to pull image: {e.message}')

        await self.middleware.call('container.image.clear_update_flag_for_tag', f'{data["from_image"]}:{data["tag"]}')

        return response

    @accepts(
        Str('id'),
        Dict(
            'options',
            Bool('force', default=False),
        )
    )
    @returns()
    async def do_delete(self, id, options):
        """
        `options.force` should be used to force delete an image even if it's in use by a stopped container.
        """
        await self.docker_checks()
        image = await self.get_instance(id)
        if image['system_image']:
            raise CallError(f'{id} is being used by system and cannot be deleted.')

        async with aiodocker.Docker() as docker:
            await docker.images.delete(name=id, force=options['force'])

        await self.middleware.call('container.image.remove_image_from_cache', image)

    @private
    async def docker_checks(self):
        if not await self.middleware.call('service.started', 'docker'):
            raise CallError('Docker service is not running')

    @private
    def normalise_tag(self, tag):
        tags = [tag]
        i = tag.find('/')
        if i == -1 or (not any(c in tag[:i] for c in ('.', ':')) and tag[:i] != 'localhost'):
            for registry in (DEFAULT_DOCKER_REGISTRY, 'docker.io'):
                tags.append(f'{registry}/{tag}')
                if '/' not in tag:
                    tags.append(f'{registry}/{DEFAULT_DOCKER_REPO}/{tag}')
        else:
            if tag.startswith('docker.io/'):
                tags.append(f'{DEFAULT_DOCKER_REGISTRY}/{tag[len("docker.io/"):]}')
            elif tag.startswith(DEFAULT_DOCKER_REGISTRY):
                tags.append(f'docker.io/{tag[len(DEFAULT_DOCKER_REGISTRY):]}')
        return tags

    @private
    def get_system_images_tags(self):
        # TODO: Let's add coredns/pause image etc

        images = [
            'nvidia/k8s-device-plugin:1.0.0-beta6',
            'k8s.gcr.io/sig-storage/csi-node-driver-registrar:v2.1.0',
            'k8s.gcr.io/sig-storage/csi-provisioner:v2.1.0',
            'k8s.gcr.io/sig-storage/csi-resizer:v1.1.0',
            'k8s.gcr.io/sig-storage/snapshot-controller:v4.0.0',
            'k8s.gcr.io/sig-storage/csi-snapshotter:v4.0.0',
            'openebs/zfs-driver:ci',
        ]
        return list(itertools.chain(
            *[self.normalise_tag(tag) for tag in images]
        ))