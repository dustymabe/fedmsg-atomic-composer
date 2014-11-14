# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import copy
import json
import shutil
import tempfile
import subprocess
import pkg_resources
import fedmsg.consumers

from datetime import datetime
from mako.template import Template
from twisted.internet import reactor


class AtomicComposer(fedmsg.consumers.FedmsgConsumer):
    """A fedmsg-driven atomic ostree composer.

    This consumer runs in the fedmsg-hub and reacts to whenever repositories
    sync to the master mirror. When this happens we trigger the
    rpm-ostree-toolbox taskrunner to kick off a treecompose by touching a file
    under the `touch_dir`. We then monitor the output of the compose using the
    systemd journal and upon completion perform various post-compose actions.
    """

    def __init__(self, hub, *args, **kw):
        # Map all of the options from our /etc/fedmsg.d config to self
        for key, item in hub.config.items():
            setattr(self, key, item)

        super(AtomicComposer, self).__init__(hub, *args, **kw)

    def consume(self, msg):
        """Called with each incoming fedmsg.

        From here we trigger an rpm-ostree compose by touching a specific file
        under the `touch_dir`. Then our `doRead` method is called with the
        output of the rpm-ostree-toolbox treecompose, which we monitor to
        determine when it has completed.
        """
        self.log.info(msg)
        body = msg['body']
        topic = body['topic']
        repo = None

        if 'rawhide' in topic:
            arch = body['msg']['arch']
            self.log.info('New rawhide %s compose ready', arch)
            repo = 'rawhide'
        elif 'branched' in topic:
            arch = body['msg']['arch']
            branch = body['msg']['branch']
            self.log.info('New %s %s branched compose ready', branch, arch)
            log = body['msg']['log']
            if log != 'done':
                self.log.warn('Compose not done?')
                return
            repo = branch
        elif 'updates.fedora' in topic:
            self.log.info('New Fedora %(release)s %(repo)s compose ready',
                          body['msg'])
            repo = 'f%(release)s-%(repo)s' % body['msg']
        else:
            self.log.warn('Unknown topic: %s', topic)

        # Copy of the release dict and expand some paths
        release = copy.deepcopy(self.releases[repo])
        release['tmp_dir'] = tempfile.mkdtemp()
        for key in ('output_dir', 'log_dir'):
            release[key] = getattr(self, key).format(**release)

        reactor.callInThread(self.compose, release)

    def compose(self, release):
        self.update_configs(release)
        self.generate_mock_config(release)
        self.init_mock(release)
        self.ostree_init(release)
        self.ostree_compose(release)
        self.update_ostree_summary(release)
        self.cleanup(release)

    def cleanup(self, release):
        """Cleanup any temporary files after the compose"""
        shutil.rmtree(release['tmp_dir'])

    def update_configs(self, release):
        """ Update the fedora-atomic.git repositories for a given release """
        git_dir = release['git_dir'] = os.path.join(release['tmp_dir'],
                os.path.basename(self.git_repo))
        self.call(['git', 'clone', '-b', release['git_branch'],
                   self.git_repo, git_dir])

    def mock_cmd(self, release, cmd):
        """Run a mock command in the chroot for a given release"""
        cmd = isinstance(cmd, list) and cmd or [cmd]
        out, err, code = self.call(['/usr/bin/mock', '-r', release['mock'],
                                    '--configdir=' + release['mock_dir']] + cmd)
        self.log.debug(out)

    def init_mock(self, release):
        """Initialize/update our mock chroot"""
        root = '/var/lib/mock/%s' % release['mock']
        if not os.path.isdir(root):
            self.mock_cmd(release, '--init')
            self.log.info('mock chroot initialized')
        else:
            self.mock_cmd(release, '--update')
            self.log.info('mock chroot updated')

    def generate_mock_config(self, release):
        """Dynamically generate our mock configuration"""
        mock_tmpl = pkg_resources.resource_string(__name__, 'templates/mock.mako')
        mock_dir = release['mock_dir'] = os.path.join(release['tmp_dir'], 'mock')
        mock_cfg = os.path.join(release['mock_dir'], release['mock'] + '.cfg')
        os.mkdir(mock_dir)
        for cfg in ('site-defaults.cfg', 'logging.ini'):
            os.symlink('/etc/mock/%s' % cfg, os.path.join(mock_dir, cfg))
        with file(mock_cfg, 'w') as cfg:
            cfg.write(Template(mock_tmpl).render(**release))

    def mock_shell(self, release, cmd):
        self.mock_cmd(release, ['--shell', cmd])

    def ostree_init(self, release):
        base = os.path.dirname(release['output_dir'])
        if not os.path.isdir(base):
            self.log.info('Creating %s', base)
            os.makedirs(base, mode=0755)
        if not os.path.isdir(release['log_dir']):
            os.makedirs(release['log_dir'])
        out = os.path.join(base, release['tree'])
        if not os.path.isdir(out):
            cmd = 'ostree init --repo=%s --mode=archive-z2 >%s 2>&1'
            logfile = os.path.join(release['log_dir'], 'ostree.log')
            self.mock_shell(release, cmd % (out, logfile))

    def ostree_compose(self, release):
        logfile = os.path.join(release['log_dir'], 'rpm-ostree.log')
        start = datetime.utcnow()
        cmd = 'rpm-ostree compose tree --repo=%s %s >%s 2>&1'
        treefile = os.path.join(release['git_dir'], 'treefile.json')
        with file(treefile, 'w') as tree:
            json.dump(release['treefile'], tree)
        self.mock_shell(release, cmd % (release['output_dir'], treefile, logfile))
        self.log.info('rpm-ostree compose complete (%s)',
                      datetime.utcnow() - start)

    def call(self, cmd, **kwargs):
        self.log.info('Running %s', cmd)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, **kwargs)
        out, err = p.communicate()
        if err:
            self.log.error(err)
        if p.returncode != 0:
            self.log.error('returncode = %d' % p.returncode)
        return out, err, p.returncode

    def update_ostree_summary(self, release):
        """Update the ostree summary file and return a path to it"""
        self.log.info('Updating the ostree summary for %s', release['name'])
        cmd = 'ostree --repo=%s summary --update' % release['output_dir']
        self.mock_shell(release, cmd)
        return os.path.join(release['output_dir'], 'summary')
