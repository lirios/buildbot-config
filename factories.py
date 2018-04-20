# -*- python -*-
# ex: set filetype=python:

from buildbot.process.build import Build
from buildbot.plugins import *
from buildbot.steps.worker import CompositeStepMixin
from buildbot import locks
from twisted.python import log
from twisted.internet import defer

import buildbot
import os.path
import json

from liribotcfg import utils


class ArchLinuxBuildStep(steps.BuildStep, CompositeStepMixin):
    """
    Build step to build the ArchLinux packages.
    """

    def __init__(self, triggers=None, **kwargs):
        steps.BuildStep.__init__(self, haltOnFailure=True, **kwargs)

    @defer.inlineCallbacks
    def run(self):
        builddir = self.build.properties.getProperty('builddir')
        content = yield self.getFileContentFromWorker('channels.json')
        if content is None:
            channels = {'stable': [], 'unstable': []}
        else:
            channels = utils.json_to_ascii(json.loads(content))
        packages_list = channels['unstable']
        packages_list.reverse()
        for name in packages_list:
            self.build.addStepsAfterCurrentStep([
                steps.ShellCommand(
                    name='build ' + name,
                    haltOnFailure=True,
                    command=['../docker-build'],
                    workdir=os.path.join(self.workdir, name),
                )
            ])
        defer.returnValue(buildbot.process.results.SUCCESS)


class ArchPackagesBuildFactory(util.BuildFactory):
    """
    Build factory for ArchLinux packages.
    """

    def __init__(self, triggers, *args, **kwargs):
        util.BuildFactory.__init__(self, *args, **kwargs)
        steps_list = [
            steps.Git(
                name='checkout sources',
                codebase=util.Property('codebase'),
                repourl=util.Property('repository'),
                branch=util.Property('branch'),
                mode='incremental',
                submodules=True,
                shallow=True,
            ),
            steps.ShellCommand(name='create database', command=['repo-add', '/repo//liri-unstable.db.tar.gz']),
            ArchLinuxBuildStep(name='select packages')
        ]
        for info in triggers:
            if 'packages' in info.get('tags', []):
                url = 'https://registry.hub.docker.com/u/%(name)s/trigger/%(token)s/' % info
                steps_list.append(
                    steps.POST(
                        name='trigger rebuild %(name)s' % info,
                        url=url,
                        headers={'Content-type': 'application/json'},
                        data={'build': True}
                    )
                )
        self.addSteps(steps_list)


class ArchISOBuildFactory(util.BuildFactory):
    """
    Build factory for ArchLinux ISO images.
    """

    def __init__(self, *args, **kwargs):
        util.BuildFactory.__init__(self, *args, **kwargs)
        self.addSteps([
            steps.Git(
                name='checkout sources',
                codebase=util.Property('codebase'),
                repourl=util.Property('repository'),
                branch=util.Property('branch'),
                mode='incremental',
                submodules=True,
                shallow=True,
            ),
            steps.ShellCommand(
                name='build image',
                haltOnFailure=True,
                command=['sudo', './build.sh', '-v', '-o', '/repo/images/nightly/'],
                workdir=os.path.join(self.workdir, 'livecd'),
            ),
            steps.ShellCommand(
                name='clean up',
                command=['sudo', 'rm', '-rf', 'work'],
                workdir=os.path.join(self.workdir, 'livecd'),
            ),
            steps.ShellCommand(
                name='remove old images',
                command=['bash', '-c', 'find /repo/images/nightly -type f -mtime +7 -exec rm {} \;'],
            )
        ])


class DockerHubBuildFactory(util.BuildFactory):
    """
    Build factory that triggers a rebuild of the Docker
    images used by this continous integration.
    """
    def __init__(self, triggers, tags, *args, **kwargs):
        util.BuildFactory.__init__(self, *args, **kwargs)
        steps_list = []
        for info in triggers:
            build = False
            available_tags = info.get('tags', [])
            for tag in tags:
                if tag in available_tags:
                    build = True
            if build is True:
                url = 'https://registry.hub.docker.com/u/%(name)s/trigger/%(token)s/' % info
                steps_list.append(steps.POST(name='trigger rebuild %(name)s' % info, url=url, headers={'Content-type': 'application/json'}, data={'build': True}))
        if len(steps_list) > 0:
            self.addSteps(steps_list)


class FlatpakFactory(util.BuildFactory):
    """
    Build factory for Flatpak.
    """

    def __init__(self, metadata, gpg_key, *args, **kwargs):
        util.BuildFactory.__init__(self, *args, **kwargs)
        repo_path = '/build/repo'
        self.addSteps([
            steps.ShellCommand(
                name='import private gpg key',
                haltOnFailure=True,
                command=['sudo', 'gpg', '--import', '/build/key.gpg'],
                workdir=self.workdir,
            ),
            steps.ShellCommand(
                name='list gpg keys',
                haltOnFailure=True,
                command=['sudo', 'gpg', '--list-keys'],
                workdir=self.workdir,
            ),
            steps.Git(
                name='checkout sources',
                codebase=util.Property('codebase'),
                repourl=util.Property('repository'),
                branch=util.Property('branch'),
                mode='incremental',
                submodules=True,
                shallow=True,
            ),
            steps.ShellCommand(
                name='build runtime',
                haltOnFailure=True,
                command=['sudo', './flatpak-build', '--repo=' + repo_path, 'build', '--metadata=' + metadata, '--type=runtime', '--gpg-key=' + gpg_key],
                workdir=self.workdir,
            ),
            steps.ShellCommand(
                name='export',
                haltOnFailure=True,
                command=['sudo', './flatpak-build', 'export', '--gpg-key=' + gpg_key],
                workdir=self.workdir,
            ),
            steps.ShellCommand(
                name='build apps',
                haltOnFailure=True,
                command=['sudo', './flatpak-build', '--repo=' + repo_path, 'build', '--metadata=' + metadata, '--type=app', '--gpg-key=' + gpg_key],
                workdir=self.workdir,
            ),
            steps.ShellCommand(
                name='synchronize to repo',
                haltOnFailure=True,
                command=['sudo', './flatpak-build', 'sync', '--dest=/flatpak/repo'],
                workdir=self.workdir,
            ),
            steps.ShellCommand(
                name='create runtime files',
                haltOnFailure=True,
                command=['sudo', './flatpak-build', 'files', '--metadata=' + metadata, '--type=runtime', '--gpg-key=' + gpg_key, '--dest=/flatpak/files'],
                workdir=self.workdir,
            ),
            steps.ShellCommand(
                name='create apps files',
                haltOnFailure=True,
                command=['sudo', './flatpak-build', 'files', '--metadata=' + metadata, '--type=app', '--gpg-key=' + gpg_key, '--dest=/flatpak/files'],
                workdir=self.workdir,
            ),
        ])
