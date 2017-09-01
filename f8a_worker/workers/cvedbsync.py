import os
from selinon import StoragePool
from f8a_worker.base import BaseTask
from f8a_worker.solver import get_ecosystem_solver, NpmDependencyParser
from f8a_worker.utils import TimedCommand, tempdir
from f8a_worker.workers import CVEcheckerTask


class CVEDBSyncTask(BaseTask):
    """ Update vulnerability sources """

    def update_dep_check_db(self, data_dir):
        depcheck = os.path.join(os.environ['OWASP_DEP_CHECK_PATH'], 'bin', 'dependency-check.sh')
        self.log.debug('Updating OWASP Dependency-Check CVE DB')
        TimedCommand.get_command_output([depcheck, '--updateonly', '--data', data_dir],
                                        timeout=1800)

    def components_to_scan(self, previous_sync_timestamp, only_already_scanned):
        """
        Get components (e:p:v) that were recently (since previous_sync_timestamp) updated
        in OSS Index, which means that they can contain new vulnerabilities.

        :param previous_sync_timestamp: timestamp of previous check
        :param only_already_scanned: include already scanned components only
        :return: generator of e:p:v
        """
        to_scan = []
        for ecosystem in ['npm', 'nuget']:  # TODO: maven
            ecosystem_solver = get_ecosystem_solver(self.storage.get_ecosystem(ecosystem),
                                                    with_parser=NpmDependencyParser())
            self.log.debug("Retrieving new %s vulnerabilities from OSS Index", ecosystem)
            ossindex_updated_packages = CVEcheckerTask.\
                query_ossindex_vulnerability_fromtill(ecosystem=ecosystem,
                                                      from_time=previous_sync_timestamp)
            for ossindex_updated_package in ossindex_updated_packages:
                package_name = ossindex_updated_package['name']
                package_affected_versions = set()
                for vulnerability in ossindex_updated_package.get('vulnerabilities', []):
                    for version_string in vulnerability.get('versions', []):
                        version_string = version_string.replace(' | ', ' || ')  # '|' work-around
                        try:
                            resolved_versions = ecosystem_solver.\
                                solve(["{} {}".format(package_name, version_string)],
                                      all_versions=True)
                        except:
                            self.log.exception("Failed to resolve %r for %s:%s", version_string,
                                               ecosystem, package_name)
                            continue
                        resolved_versions = resolved_versions.get(package_name, [])
                        if only_already_scanned:
                            already_scanned_versions =\
                                [ver for ver in resolved_versions if
                                 self.storage.get_analysis_count(ecosystem, package_name, ver) > 0]
                            package_affected_versions.update(already_scanned_versions)
                        else:
                            package_affected_versions.update(resolved_versions)

                for version in package_affected_versions:
                    to_scan.append({
                        'ecosystem': ecosystem,
                        'name': package_name,
                        'version': version
                        })
        msg = "Components to be {}scanned for vulnerabilities:".\
            format("re-" if only_already_scanned else "")
        self.log.debug(msg)
        self.log.debug(to_scan)
        return to_scan

    def execute(self, arguments):
        """

        :param arguments: optional argument 'only_already_scanned' to run only on already analysed packages
        :return: EPV dict describing which packages should be analysed
        """
        only_already_scanned = arguments.pop('only_already_scanned', True) if arguments else True
        ignore_modification_time = arguments.pop('ignore_modification_time', False) if arguments else False
        self._strict_assert(not arguments)

        s3 = StoragePool.get_connected_storage('S3VulnDB')

        # Update OWASP Dependency-check DB on S3
        with tempdir() as temp_data_dir:
            s3.retrieve_depcheck_db_if_exists(temp_data_dir)
            self.update_dep_check_db(temp_data_dir)
            s3.store_depcheck_db(temp_data_dir)

        self.log.debug('Updating sync associated metadata')
        previous_sync_timestamp = s3.update_sync_date()
        if ignore_modification_time:
            previous_sync_timestamp = 0
        # get components which might have new vulnerabilities since previous sync
        to_scan = self.components_to_scan(previous_sync_timestamp, only_already_scanned)
        return {'modified': to_scan}
