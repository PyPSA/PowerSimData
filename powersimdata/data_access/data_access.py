import pickle
import posixpath
from contextlib import contextmanager
from subprocess import Popen

import fs
from fs.multifs import MultiFS
from fs.path import basename, dirname
from fs.tempfs import TempFS
from scipy.io import savemat

from powersimdata.data_access.profile_helper import (
    get_profile_version_cloud,
    get_profile_version_local,
)
from powersimdata.data_access.ssh_fs import WrapSSHFS
from powersimdata.utility import server_setup


def get_blob_fs(container):
    account = "besciences"
    return fs.open_fs(f"azblob://{account}@{container}")


def get_ssh_fs(root=""):
    host = server_setup.SERVER_ADDRESS
    port = server_setup.SERVER_SSH_PORT
    username = server_setup.get_server_user()
    base_fs = fs.open_fs(f"ssh://{username}@{host}:{port}")
    return WrapSSHFS(base_fs, root)


def get_multi_fs(root):
    """Create filesystem combining the server (if connected) with profile and scenario
    containers in blob storage. The priority is in descending order, so the server will
    be used first if possible
    """
    scenario_data = get_blob_fs("scenariodata")
    profiles = get_blob_fs("profiles")
    mfs = MultiFS()
    try:
        ssh_fs = get_ssh_fs(root)
        mfs.add_fs("ssh_fs", ssh_fs, write=True, priority=3)
    except:  # noqa
        print("Could not connect to ssh server")
    mfs.add_fs("profile_fs", profiles, priority=2)
    mfs.add_fs("scenario_fs", scenario_data, priority=1)
    return mfs


class DataAccess:
    """Interface to a local or remote data store."""

    def __init__(self, root):
        """Constructor"""
        self.root = root
        self.join = fs.path.join
        self.local_fs = None

    @contextmanager
    def get(self, filepath):
        """Copy file from remote filesystem if needed and read into memory

        :param str filepath: path to file
        :return: (*tuple*) -- file object and filepath to be handled by caller
        """
        if not self.local_fs.exists(filepath):
            print(f"{filepath} not found on local machine")
            from_dir, filename = dirname(filepath), basename(filepath)
            self.copy_from(filename, from_dir)

        with self.local_fs.openbin(filepath) as f:
            filepath = self.local_fs.getsyspath(filepath)
            yield f, filepath

    def write(self, filepath, data, save_local=True, callback=None):
        """Write a file to data store.

        :param str filepath: path to save data to
        :param object data: data to save
        :param bool save_local: whether a copy should also be saved to the local filesystem, if
            such a filesystem is configured. Defaults to True.
        :param callable callback: the specific persistence implementation
        """
        self._check_file_exists(filepath, should_exist=False)
        if callback is None:
            callback = self._callback

        print("Writing %s" % filepath)
        self._write(self.fs, filepath, data, callback)
        if save_local:
            self._write(self.local_fs, filepath, data, callback)

    def _write(self, fs, filepath, data, callback=None):
        """Write a file to given data store.

        :param fs.base.FS fs: pyfilesystem to which to write data
        :param str filepath: path to save data to
        :param object data: data to save
        :param callable callback: the specific persistence implementation
        :raises ValueError: if extension is unknown.
        """
        fs.makedirs(dirname(filepath), recreate=True)

        with fs.openbin(filepath, "w") as f:
            callback(f, filepath, data)

    def _callback(self, f, filepath, data):
        ext = basename(filepath).split(".")[-1]
        if ext == "pkl":
            pickle.dump(data, f)
        elif ext == "csv":
            data.to_csv(f)
        elif ext == "mat":
            savemat(f, data, appendmat=False)
        else:
            raise ValueError("Unknown extension! %s" % ext)

    def copy_from(self, file_name, from_dir=None):
        """Copy a file from data store to userspace.

        :param str file_name: file name to copy.
        :param str from_dir: data store directory to copy file from.
        """
        from_dir = "" if from_dir is None else from_dir
        from_path = self.join(from_dir, file_name)
        self._check_file_exists(from_path, should_exist=True)

        location, _ = self.fs.which(from_path)
        print(f"Transferring {file_name} from {location}")
        with TempFS() as tmp_fs:
            self.local_fs.makedirs(from_dir, recreate=True)
            tmp_fs.makedirs(from_dir, recreate=True)
            fs.copy.copy_file(self.fs, from_path, tmp_fs, from_path)
            fs.move.move_file(tmp_fs, from_path, self.local_fs, from_path)

    def tmp_folder(self, scenario_id):
        """Get path to temporary scenario folder

        :param int/str scenario_id: the scenario id
        :return: (*str*) -- the specified path
        """
        return self.join(server_setup.EXECUTE_DIR, f"scenario_{scenario_id}")

    def copy(self, src, dest):
        """Copy file to new location

        :param str src: path to file
        :param str dest: destination folder
        """
        if self.fs.isdir(dest):
            dest = self.join(dest, fs.path.basename(src))

        self.fs.copy(src, dest)

    def remove(self, pattern, confirm=True):
        """Delete files in current environment

        :param str pattern: glob specifying files to remove
        :param bool confirm: prompt before executing command
        """
        if confirm:
            confirmed = input(f"Delete '{pattern}'? [y/n] (default is 'n')")
            if confirmed.lower() != "y":
                print("Operation cancelled.")
                return
        self.fs.glob(pattern).remove()
        print("--> Done!")

    def _check_file_exists(self, path, should_exist=True):
        """Check that file exists (or not) at the given path

        :param str path: the relative path to the file
        :param bool should_exist: whether the file is expected to exist
        :raises OSError: if the expected condition is not met
        """
        location, _ = self.fs.which(path)
        exists = location is not None
        if should_exist and not exists:
            remotes = [f[0] for f in self.fs.iterate_fs()]
            raise OSError(f"{path} not found on any of {remotes}")
        if not should_exist and exists:
            raise OSError(f"{path} already exists on {location}")

    def get_profile_version(self, grid_model, kind):
        """Returns available raw profile from blob storage

        :param str grid_model: grid model.
        :param str kind: *'demand'*, *'hydro'*, *'solar'* or *'wind'*.
        :return: (*list*) -- available profile version.
        """
        blob_version = get_profile_version_cloud(grid_model, kind)
        local_version = get_profile_version_local(grid_model, kind)
        return list(set(blob_version + local_version))

    def checksum(self, relative_path):
        """Return the checksum of the file path

        :param str relative_path: path relative to root
        :return: (*str*) -- the checksum of the file
        """
        return self.fs.hash(relative_path, "sha256")

    def push(self, file_name, checksum, rename):
        """Push the file from local to remote root folder, ensuring integrity

        :param str file_name: the file name, located at the local root
        :param str checksum: the checksum prior to download
        :param str rename: the new filename
        """
        raise NotImplementedError


class LocalDataAccess(DataAccess):
    """Interface to shared data volume"""

    def __init__(self, root=server_setup.LOCAL_DIR):
        super().__init__(root)
        self.local_fs = fs.open_fs(root)
        self.fs = self._get_fs()

    def _get_fs(self):
        mfs = MultiFS()
        profiles = get_blob_fs("profiles")
        mfs.add_fs("profile_fs", profiles, priority=2)
        mfs.add_fs("local_fs", self.local_fs, write=True, priority=3)
        return mfs

    def push(self, file_name, checksum, rename):
        """Rename the file.

        :param str file_name: the file name, located at the local root
        :param str checksum: the checksum prior to download
        :param str rename: the new filename
        """
        if checksum != self.checksum(rename):
            raise ValueError("Checksums do not match")
        self.fs.move(file_name, rename, overwrite=True)


class SSHDataAccess(DataAccess):
    """Interface to a remote data store, accessed via SSH."""

    def __init__(self, root=server_setup.DATA_ROOT_DIR):
        """Constructor"""
        super().__init__(root)
        self._fs = None
        self.local_fs = fs.open_fs(server_setup.LOCAL_DIR)

    @property
    def fs(self):
        """Get or create the filesystem object

        :raises IOError: if connection failed or still within retry window
        :return: (*fs.multifs.MultiFS*) -- filesystem instance
        """
        if self._fs is None:
            self._fs = get_multi_fs(self.root)
        return self._fs

    def execute_command_async(self, command):
        """Execute a command via ssh, without waiting for completion.

        :param list command: list of str to be passed to command line.
        :return: (*subprocess.Popen*) -- the local ssh process
        """
        username = server_setup.get_server_user()
        cmd_ssh = ["ssh", username + "@" + server_setup.SERVER_ADDRESS]
        full_command = cmd_ssh + command
        process = Popen(full_command)
        return process

    def checksum(self, relative_path):
        """Return the checksum of the file path

        :param str relative_path: path relative to root
        :return: (*str*) -- the checksum of the file
        """
        self._check_file_exists(relative_path)
        full_path = self.join(self.root, relative_path)
        ssh_fs = self.fs.get_fs("ssh_fs")
        return ssh_fs.checksum(full_path)

    def push(self, file_name, checksum, rename):
        """Push file to server and verify the checksum matches a prior value

        :param str file_name: the file name, located at the local root
        :param str checksum: the checksum prior to download
        :param str rename: the new filename
        :raises IOError: if command generated stderr
        """
        backup = f"{rename}.temp"

        self._check_file_exists(backup, should_exist=False)
        print(f"Transferring {rename} to server")
        fs.move.move_file(self.local_fs, file_name, self.fs, backup)

        values = {
            "original": posixpath.join(self.root, rename),
            "updated": posixpath.join(self.root, backup),
            "lockfile": posixpath.join(self.root, "scenario.lockfile"),
            "checksum": checksum,
        }

        template = "(flock -x 200; \
                prev='{checksum}'; \
                curr=$(sha1sum {original}); \
                if [[ $prev == $curr ]]; then mv {updated} {original} -b; \
                else echo CONFLICT_ERROR 1>&2; fi) \
                200>{lockfile}"

        command = template.format(**values)
        _, _, stderr = self.fs.exec_command(command)

        errors = stderr.readlines()
        if len(errors) > 0:
            for e in errors:
                print(e)
            raise IOError("Failed to push file - most likely a conflict was detected.")


class MemoryDataAccess(SSHDataAccess):
    """Mimic a client server architecture using in memory filesystems"""

    def __init__(self):
        self.local_fs = fs.open_fs("mem://")
        self._fs = self._get_fs()
        self.root = "foo"
        self.join = fs.path.join

    def _get_fs(self):
        mfs = MultiFS()
        mfs.add_fs("in_memory", fs.open_fs("mem://"), write=True)
        return mfs

    def push(self, file_name, checksum, rename):
        """Push file from local to remote filesystem, bypassing checksum since this is
        in memory.

        :param str file_name: the file name, located at the local root
        :param str checksum: the checksum prior to download
        :param str rename: the new filename
        """
        fs.move.move_file(self.local_fs, file_name, self.fs, rename)
