import sys
import logging

from glob import glob
from subprocess import run
from refgenconf import RefGenConf
from refgenconf.exceptions import GenomeConfigFormatError, ConfigNotCompliantError
from ubiquerg import checksum, size, is_command_callable

from .const import *

global _LOGGER
_LOGGER = logging.getLogger(PKG_NAME)


def archive(rgc, registry_paths, force, remove, cfg_path, genomes_desc):
    """
    Takes the RefGenConf object and builds individual tar archives
    that can be then served with 'refgenieserver serve'. Additionally determines their md5 checksums, file sizes and
    updates the original refgenie config with these data. If the --asset and/or --genome options  are used (specific
    build is requested) the archiver will check for the existence of config file saved in the path provided in
    `genome_server` in the original config and update it so that no archive metadata is lost

    :param RefGenConf rgc: configuration object with the data to build the servable archives for
    :param list[dict] registry_paths: a collection of mappings that identifies the assets to update
    :param bool force: whether to force the build of archive, regardless of its existence
    :param bool remove: whether remove specified genome/asset:tag from the archive
    :param str cfg_path: config file path
    """
    if float(rgc[CFG_VERSION_KEY]) < float(REQ_CFG_VERSION):
        raise ConfigNotCompliantError("You need to update the genome config to v{} in order to use the archiver. "
                                      "The required version can be generated with refgenie >= {}".
                                      format(REQ_CFG_VERSION, REFGENIE_BY_CFG[REQ_CFG_VERSION]))
    try:
        server_rgc_path = os.path.join(rgc[CFG_ARCHIVE_KEY], os.path.basename(cfg_path))
    except KeyError:
        raise GenomeConfigFormatError("The config '{}' is missing a '{}' entry. Can't determine the desired archive.".
                                      format(cfg_path, CFG_ARCHIVE_KEY))
    if force:
        _LOGGER.info("build forced; file existence will be ignored")
        if os.path.exists(server_rgc_path):
            _LOGGER.debug("'{}' file was found and will be updated".format(server_rgc_path))
    _LOGGER.debug("registry_paths: {}".format(registry_paths))

    # original RefGenConf has been created in read-only mode,
    # make it RW compatible and point to new target path for server use or initialize a new object
    if os.path.exists(server_rgc_path):
        rgc_server = RefGenConf(filepath=server_rgc_path, writable=True)
        if remove:
            if not registry_paths:
                _LOGGER.error("To remove archives you have to specify them. Use 'asset_registry_path' argument.")
                exit(1)
            _remove_archive(rgc_server, registry_paths)
            rgc_server.write()
            exit(0)
    else:
        if remove:
            _LOGGER.error("You can't remove archives since the genome_archive path does not exist yet.")
            exit(1)
        rgc_server = rgc.make_writable(server_rgc_path)

    if registry_paths:
        genomes, asset_list, tag_list = _get_paths_element(registry_paths, "namespace"), \
                                        _get_paths_element(registry_paths, "item"), _get_paths_element(registry_paths,
                                                                                                       "tag")
    else:
        genomes = rgc.genomes_list()
        asset_list, tag_list = None, None
    if not genomes:
        _LOGGER.error("No genomes found")
        exit(1)
    else:
        _LOGGER.debug("Genomes to be processed: {}".format(str(genomes)))
    if genomes_desc is not None:
        if os.path.exists(genomes_desc):
            import csv
            _LOGGER.info("Found a genomes descriptions CSV file: {}".format(genomes_desc))
            with open(genomes_desc, mode='r') as infile:
                reader = csv.reader(infile)
                descs = {rows[0]: rows[1] for rows in reader}
        else:
            _LOGGER.warning("Genomes descriptions CSV file does not exist: {}".format(genomes_desc))
    counter = 0
    for genome in genomes:
        genome_dir = os.path.join(rgc[CFG_FOLDER_KEY], genome)
        target_dir = os.path.join(rgc[CFG_ARCHIVE_KEY], genome)
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
        genome_desc = descs[genome] if genomes_desc is not None and genome in descs else \
            rgc[CFG_GENOMES_KEY][genome].setdefault(CFG_GENOME_DESC_KEY, DESC_PLACEHOLDER)
        genome_checksum = rgc[CFG_GENOMES_KEY][genome].setdefault(CFG_CHECKSUM_KEY, CHECKSUM_PLACEHOLDER)
        genome_attrs = {CFG_GENOME_DESC_KEY: genome_desc,
                        CFG_CHECKSUM_KEY: genome_checksum}
        rgc_server.update_genomes(genome, genome_attrs)
        _LOGGER.debug("updating '{}' genome attributes...".format(genome))
        asset = asset_list[counter] if asset_list is not None else None
        assets = asset or rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY].keys()
        if not assets:
            _LOGGER.error("No assets found")
            continue
        else:
            _LOGGER.debug("Assets to be processed: {}".format(str(assets)))
        for asset_name in assets if isinstance(assets, list) else [assets]:
            asset_desc = rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset_name].setdefault(CFG_ASSET_DESC_KEY,
                                                                                             DESC_PLACEHOLDER)
            default_tag = rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset_name].setdefault(CFG_ASSET_DEFAULT_TAG_KEY,
                                                                                              DEFAULT_TAG)
            asset_attrs = {CFG_ASSET_DESC_KEY: asset_desc,
                           CFG_ASSET_DEFAULT_TAG_KEY: default_tag}
            _LOGGER.debug("updating '{}/{}' asset attributes...".format(genome, asset_name))
            rgc_server.update_assets(genome, asset_name, asset_attrs)
            tag = tag_list[counter] if tag_list is not None else None
            tags = tag or rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset_name][CFG_ASSET_TAGS_KEY].keys()
            for tag_name in tags if isinstance(tags, list) else [tags]:
                if not rgc.is_asset_complete(genome, asset_name, tag_name):
                    _LOGGER.info("'{}/{}:{}' is incomplete, skipping".format(genome, asset_name, tag_name))
                    rgc_server.remove_assets(genome, asset_name, tag_name)
                    continue
                rgc_server.write()
                del rgc_server
                file_name = rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset_name][CFG_ASSET_TAGS_KEY][tag_name][
                    CFG_ASSET_PATH_KEY]
                target_file = os.path.join(target_dir, "{}__{}".format(asset_name, tag_name) + ".tgz")
                input_file = os.path.join(genome_dir, file_name, tag_name)
                # these attributes have to be read from the original RefGenConf in case the archiver just increments
                # an existing server RefGenConf
                parents = rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset_name][CFG_ASSET_TAGS_KEY][tag_name]. \
                    setdefault(CFG_ASSET_PARENTS_KEY, [])
                children = rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset_name][CFG_ASSET_TAGS_KEY][tag_name]. \
                    setdefault(CFG_ASSET_CHILDREN_KEY, [])
                seek_keys = rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset_name][CFG_ASSET_TAGS_KEY][tag_name]. \
                    setdefault(CFG_SEEK_KEYS_KEY, {})
                asset_digest = rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset_name][CFG_ASSET_TAGS_KEY][tag_name]. \
                    setdefault(CFG_ASSET_CHECKSUM_KEY, None)
                if not os.path.exists(target_file) or force:
                    _LOGGER.info("creating asset '{}' from '{}'".format(target_file, input_file))
                    try:
                        _check_tgz(input_file, target_file, asset_name)
                        _copy_recipe(input_file, target_dir, asset_name, tag_name)
                        _copy_log(input_file, target_dir, asset_name, tag_name)
                    except OSError as e:
                        _LOGGER.warning(e)
                        rgc_server = RefGenConf(filepath=server_rgc_path, writable=True)
                        continue
                    else:
                        rgc_server = RefGenConf(filepath=server_rgc_path, writable=True)
                        _LOGGER.info("updating '{}/{}:{}' tag attributes...".format(genome, asset_name, tag_name))
                        tag_attrs = {CFG_ASSET_PATH_KEY: file_name,
                                     CFG_SEEK_KEYS_KEY: seek_keys,
                                     CFG_ARCHIVE_CHECKSUM_KEY: checksum(target_file),
                                     CFG_ARCHIVE_SIZE_KEY: size(target_file),
                                     CFG_ASSET_SIZE_KEY: size(input_file),
                                     CFG_ASSET_PARENTS_KEY: parents,
                                     CFG_ASSET_CHILDREN_KEY: children,
                                     CFG_ASSET_CHECKSUM_KEY: asset_digest}
                        _LOGGER.debug("attr dict: {}".format(tag_attrs))
                        rgc_server.update_tags(genome, asset_name, tag_name, tag_attrs)
                        rgc_server.write()
                else:
                    _LOGGER.debug("'{}' exists".format(target_file))
                    rgc_server = RefGenConf(filepath=server_rgc_path, writable=True)
        counter += 1
    try:
        rgc_server = _purge_nonservable(rgc_server)
    except Exception as e:
        _LOGGER.warning("Caught exception while removing non-servable config entries: {}".format(e))
    _LOGGER.info("builder finished; server config file saved to: '{}'".format(rgc_server.write(server_rgc_path)))


def _check_tgz(path, output, asset_name):
    """
    Check if file exists and tar it.
    If gzipping is requested, the availability of pigz software is checked and used.

    :param str path: path to the file to be tarred
    :param str output: path to the result file
    :param str asset_name: name of the asset
    :raise OSError: if the file/directory meant to be archived does not exist
    """
    # since the genome directory structure changed (added tag layer) in refgenie >= 0.7.0 we need to perform some
    # extra file manipulation before archiving to make the produced archive compatible with new and old versions
    # of refgenie CLI. The difference is that refgenie < 0.7.0 requires the asset to be archived with the asset-named
    # enclosing dir, but with no tag-named directory as this concept did not exist back then.
    if os.path.exists(path):
        # move the asset files to asset-named dir
        cmd = "cd {p}; mkdir {an}; mv `find . -type f -not -path './_refgenie_build*'` {an}  2>/dev/null; "
        # tar gzip
        cmd += "tar -cvf - {an} | pigz > {o}; " if is_command_callable("pigz") else "tar -cvzf {o} {an}; "
        # move the files back to the tag-named dir and remove asset-named dir
        cmd += "mv {an}/* .; rm -r {an}"
        _LOGGER.debug("command: {}".format(cmd.format(p=path, o=output, an=asset_name)))
        run(cmd.format(p=path, o=output, an=asset_name), shell=True)
    else:
        raise OSError("entity '{}' does not exist".format(path))


def _copy_log(input_dir, target_dir, asset_name, tag_name):
    """
    Copy the log file

    :param str input_dir: path to the directory to copy the recipe from
    :param str target_dir: path to the directory to copy the recipe to
    """
    log_path = "{}/_refgenie_build/refgenie_log.md".format(input_dir)
    if log_path and os.path.exists(log_path):
        run("cp " + log_path + " " +
            os.path.join(target_dir, "build_log_{}__{}.md".format(asset_name, tag_name)), shell=True)
        _LOGGER.debug("Log copied to: {}".format(target_dir))
    else:
        _LOGGER.debug("Log not found: ".format(log_path))


def _copy_recipe(input_dir, target_dir, asset_name, tag_name):
    """
    Copy the recipe

    :param str input_dir: path to the directory to copy the recipe from
    :param str target_dir: path to the directory to copy the recipe to
    :param str asset_name: asset name
    :param str tag_name: tag name
    """
    recipe_path = "{}/_refgenie_build/build_recipe_{}__{}.json". \
        format(input_dir, asset_name, tag_name)
    if recipe_path and os.path.exists(recipe_path):
        run("cp " + recipe_path + " " + target_dir, shell=True)
        _LOGGER.debug("Recipe copied to: {}".format(target_dir))
    else:
        _LOGGER.debug("Recipe not found: {}".format(recipe_path))


def _purge_nonservable(rgc):
    """
    Remove entries in RefGenConf object that were not processed by the archiver and should not be served

    :param refgenconf.RefGenConf rgc: object to check
    :return refgenconf.RefGenConf: object with just the servable entries
    """

    def _check_servable(rgc, genome, asset, tag):
        tag_data = rgc[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag]
        return all([r in tag_data for r in [CFG_ARCHIVE_CHECKSUM_KEY, CFG_ARCHIVE_SIZE_KEY]])

    for genome_name, genome in rgc[CFG_GENOMES_KEY].items():
        for asset_name, asset in genome[CFG_ASSETS_KEY].items():
            for tag_name, tag in asset[CFG_ASSET_TAGS_KEY].items():
                if not _check_servable(rgc, genome_name, asset_name, tag_name):
                    rgc.remove_assets(genome_name, asset_name, tag_name)
    return rgc


def _remove_archive(rgc, registry_paths):
    """
    Remove archives and corresponding entries from the RefGenConf object

    :param refgenconf.RefGenConf rgc: object to remove the entries from
    :param list[dict] registry_paths: entries to remove
    :return list[str]: removed file paths
    """
    ret = []
    for registry_path in _correct_registry_paths(registry_paths):
        genome, asset, tag = registry_path["namespace"], registry_path["item"], registry_path["tag"]
        try:
            if asset is None:
                [rgc.remove_assets(genome, x, None) for x in rgc.list_assets_by_genome(genome)]
            else:
                rgc.remove_assets(genome, asset, tag)
            _LOGGER.info("{}/{}:{} removed".format(genome, asset, tag))
        except KeyError:
            _LOGGER.warning("{}/{}:{} not found and not removed.".format(genome, asset, tag))
            continue
        ret.append(os.path.join(rgc[CFG_ARCHIVE_KEY], genome, "{}__{}".format(asset or "*", tag or "*") + ".tgz"))
        for p in ret:
            archives = glob(p)
            for path in archives:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    _LOGGER.warning("File does not exist: {}".format(path))
        try:
            os.removedirs(os.path.join(rgc[CFG_ARCHIVE_KEY], genome))
            _LOGGER.info("Removed empty genome directory: {}".format(genome))
        except OSError:
            pass
    return ret


def _correct_registry_paths(registry_paths):
    """
    parse_registry_path function recognizes the 'item' as the central element of the asset registry path.
    We require the 'namespace' to be the central one. Consequently, this function swaps them.

    :param list[dict] registry_paths: output of parse_registry_path
    :return list[dict]: corrected registry paths
    """

    def _swap(rp):
        """
        Swaps dict values of 'namespace' with 'item' keys

        :param dict rp: dict to swap values for
        :return dict: dict with swapped values
        """
        rp["namespace"] = rp["item"]
        rp["item"] = None
        return rp

    return [_swap(x) if x["namespace"] is None else x for x in registry_paths]


def _get_paths_element(registry_paths, element):
    """
    Extract the specific element from a collection of registry paths

    :param list[dict] registry_paths: output of parse_registry_path
    :param str element: 'protocol', 'namespace', 'item' or 'tag'
    :return list[str]: extracted elements
    """
    return [x[element] for x in _correct_registry_paths(registry_paths)]



