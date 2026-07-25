import os
import re
import copy
from pathlib import Path
import clique
import pyblish.api

from ayon_core.pipeline import (
    publish,
    get_temp_dir
)
from ayon_core.pipeline.publish.lib import get_default_reviewable_layers
import ayon_api
from ayon_core.pipeline.colorspace import get_representation_ocio_config_path
from ayon_core.lib import is_oiio_supported

from ayon_core.lib.transcoding import (
    MissingRGBAChannelsError,
    oiio_color_convert,
)

from ayon_core.lib.profiles_filtering import filter_profiles


# Simple helper: repeatedly replace plain-name placeholders like {name}
# with values from `data`, even if they appear inside other braces.
# This intentionally does NOT evaluate arithmetic expressions — it only
# substitutes simple identifiers. Example:
#   '{856*{pixel_aspect}}x550' -> '{856*1.5}x550'
def format_with_expressions(template, data):
    name_pattern = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    prev = None
    result = template
    while prev != result:
        prev = result
        def _repl_name(m):
            key = m.group(1)
            if key in data:
                val = data[key]
                return str(val)
            return m.group(0)
        result = name_pattern.sub(_repl_name, result)
    return result


class ExtractOIIOTranscode(publish.Extractor):
    """
    Extractor to convert colors from one colorspace to different.

    Expects "colorspaceData" on representation. This dictionary is collected
    previously and denotes that representation files should be converted.
    This dict contains source colorspace information, collected by hosts.

    Target colorspace is selected by profiles in the Settings, based on:
    - host names
    - product base types
    - product names
    - task types
    - task names

    Can produce one or more representations (with different extensions) based
    on output definition in format:
        "output_name: {
            "extension": "png",
            "colorspace": "ACES - ACEScg",
            "display": "",
            "view": "",
            "tags": [],
            "custom_tags": []
        }

    If 'extension' is empty original representation extension is used.
    'output_name' will be used as name of new representation. In case of value
        'passthrough' name of original representation will be used.

    'colorspace' denotes target colorspace to be transcoded into. Could be
    empty if transcoding should be only into display and viewer colorspace.
    (In that case both 'display' and 'view' must be filled.)
    """

    label = "Transcode color spaces"
    order = pyblish.api.ExtractorOrder + 0.019

    settings_category = "core"

    optional = True

    # Supported extensions
    supported_exts = {"exr", "jpg", "jpeg", "png", "dpx"}

    # Configurable by Settings
    profiles = None
    options = None

    def process(self, instance):
        if instance.data.get("farm"):
            self.log.debug("Should be processed on farm, skipping.")
            return

        if not self.profiles:
            self.log.debug("No profiles present for color transcode")
            return

        if "representations" not in instance.data:
            self.log.debug("No representations, skipping.")
            return

        if not is_oiio_supported():
            self.log.warning("OIIO not supported, no transcoding possible.")
            return

        profile = self._get_profile(instance)
        if not profile:
            return

        profile_output_defs = profile["outputs"]
        new_representations = []
        repres = instance.data["representations"]

        scene_display = instance.data.get(
            "sceneDisplay",
            # Backward compatibility
            instance.data.get("colorspaceDisplay")
        )
        scene_view = instance.data.get(
            "sceneView",
            # Backward compatibility
            instance.data.get("colorspaceView")
        )
        project_settings = instance.context.data["project_settings"]
        review_layers = get_default_reviewable_layers(project_settings)

        ################
        folder_path = instance.data.get("folderPath")
        project = os.environ["AYON_PROJECT_NAME"]
        folder = ayon_api.get_folder_by_path(project, folder_path)
        ###############

        for idx, repre in enumerate(list(repres)):
            self.log.debug("repre ({}): `{}`".format(idx + 1, repre["name"]))
            if not self._repre_is_valid(repre, profile):
                continue

            added_representations = False
            added_review = False

            colorspace_data = repre["colorspaceData"]

            config_path = get_representation_ocio_config_path(
                repre,
                anatomy=instance.context.data["anatomy"],
                logger=self.log
            )
            if not config_path:
                self.log.debug(
                    "Skipping OIIO Color Transcode because no OCIO config"
                    " path found on representation."
                )
                continue

            source_colorspace = colorspace_data["colorspace"]
            source_display = colorspace_data.get("display")
            source_view = colorspace_data.get("view")

            # Get representation files to convert
            if isinstance(repre["files"], list):
                repre_files_to_convert = copy.deepcopy(repre["files"])
            else:
                repre_files_to_convert = [repre["files"]]

            # Process each output definition
            for output_def in profile_output_defs:
                # Local copy to avoid accidental mutable changes
                files_to_convert = list(repre_files_to_convert)

                output_name = output_def["name"]
                new_repre = copy.deepcopy(repre)

                original_staging_dir = new_repre["stagingDir"]
                new_staging_dir = get_temp_dir(
                    project_name=instance.context.data["projectName"],
                    use_local_temp=True,
                )
                new_repre["stagingDir"] = new_staging_dir

                output_extension = output_def["extension"]
                output_extension = output_extension.replace('.', '')
                self._rename_in_representation(new_repre,
                                               files_to_convert,
                                               output_name,
                                               output_extension)

                transcoding_type = output_def["transcoding_type"]

                # Set target colorspace/display/view based on transcoding type
                target_colorspace = None
                target_view = None
                target_display = None
                if transcoding_type == "colorspace":
                    target_colorspace = output_def["colorspace"]
                elif transcoding_type == "display_view":
                    display_view = output_def["display_view"]
                    # If empty values are provided in output definition,
                    # fallback to scene display/view that is collected from DCC
                    target_view = display_view["view"] or scene_view
                    target_display = display_view["display"] or scene_display

                # both could be already collected by DCC,
                # but could be overwritten when transcoding
                if target_view:
                    new_repre["colorspaceData"]["view"] = target_view
                if target_display:
                    new_repre["colorspaceData"]["display"] = target_display
                if target_colorspace:
                    new_repre["colorspaceData"]["colorspace"] = \
                        target_colorspace

                additional_command_args = (output_def["oiiotool_args"]
                                           ["additional_command_args"])

                #==================
                # Get lens for shot
                #==================
                # https://regex101.com/r/Dn0fpI/1
                pattern = r"\{(?:(?P<link>\w+)\[(?P<type>[^\]]+)\]|(?P<sep>\|))\}"

                undistort_path = None

                resolved_command_args = []
                data = {}
                for arg in additional_command_args:
                    matches = []
                    index = 0
                    for match in re.finditer(pattern, arg):
                        if match.group("sep"):
                            # This handles the {|} part
                            index -= 1
                        else:
                            # This handles the {link[type]} part
                            link_val = match.group("link")
                            type_val = match.group("type")

                            try:
                                matches[index]
                            except IndexError:
                                matches.append([])

                            matches[index].append((link_val, type_val))

                            index += 1

                    if not matches:
                        resolved_command_args.append(arg)
                        continue

                    for match_alternatives in matches:
                        for match in match_alternatives:
                            link, image_type = match

                            link = ayon_api.get_folder_links(
                                project_name=project,
                                folder_id=folder["id"],
                                link_types=[link],
                            )[0]

                            if not link:
                                continue

                            product = ayon_api.get_product_by_name(project, image_type, folder_id=link["entityId"])
                            if not product:
                                continue

                            versions = list(ayon_api.get_versions(project, product_ids=[product["id"]]))
                            versions.sort(key=lambda v: v["version"])
                            latest_version = versions[-1]
                            latest_version_id = latest_version["id"]

                            representations = ayon_api.get_representations(
                                project,
                                version_ids=[latest_version_id],
                                fields=["name", "files"]
                            )
                            undistort_path = ""
                            for representation in representations:
                                if representation["name"] != "exr":
                                    continue

                                self.log.warning(latest_version)

                                undistort_path = representation["files"][0]["path"]
                                SOURCE_W = 3424
                                SOURCE_H = 2202

                                width = latest_version["attrib"]["resolutionWidth"]
                                height = latest_version["attrib"]["resolutionHeight"]

                                # Calculate the required offsets to keep it centered
                                # We use // for integer division to avoid decimals
                                off_x = (width - SOURCE_W) // 2
                                off_y = (height - SOURCE_H) // 2

                                data.update({
                                    "width": width,
                                    "height": height,
                                    "offset_x": off_x,
                                    "offset_y": off_y,
                                })

                            if not undistort_path:
                                continue

                            undistort_path = Path(undistort_path.replace("{root[work]}", os.getenv("AYON_PROJECT_ROOT_WORK")))

                            if not undistort_path.exists():
                                raise RuntimeError("undistorted path: %s is not on disk!" %  undistort_path)

                            resolved_command_args.append(str(undistort_path))

                data.update({
                    "pixel_aspect": folder["attrib"]["pixelAspect"],
                })

                if data:
                    # Apply to arguments
                    final_args = []
                    for arg in resolved_command_args:
                        if isinstance(arg, str) and "{" in arg:
                            try:
                                # Replace inner placeholders and evaluate simple
                                # arithmetic expressions while keeping outer
                                # structure intact. This handles cases like
                                # '{856*{pixel_aspect}}x550'.
                                formatted_arg = format_with_expressions(arg, data)
                                final_args.append(formatted_arg)
                            except Exception:
                                # Fallback to original argument on any error
                                final_args.append(arg)
                        else:
                            final_args.append(arg)

                    additional_command_args = final_args
                else:
                    additional_command_args = resolved_command_args

                sequence_files = self._translate_to_sequence(
                    files_to_convert)
                self.log.debug("Files to convert: {}".format(sequence_files))
                missing_rgba_review_channels = False
                for file_name in sequence_files:
                    if isinstance(file_name, clique.Collection):
                        # Support sequences with holes by supplying
                        # dedicated `--frames` argument to `oiiotool`
                        # Create `frames` string like "1001-1002,1004,1010-1012
                        # Create `filename` string like "file.#.exr"
                        frames = file_name.format("{ranges}").replace(" ", "")
                        frame_padding = file_name.padding
                        file_name = file_name.format("{head}#{tail}")
                        parallel_frames = True
                    elif isinstance(file_name, str):
                        # Single file
                        frames = None
                        frame_padding = None
                        parallel_frames = False
                    else:
                        raise TypeError(
                            f"Unsupported file name type: {type(file_name)}."
                            " Expected str or clique.Collection."
                        )

                    self.log.debug("Transcoding file: `{}`".format(file_name))
                    input_path = os.path.join(original_staging_dir, file_name)
                    output_path = self._get_output_file_path(input_path,
                                                             new_staging_dir,
                                                             output_extension)
                    try:
                        oiio_color_convert(
                            input_path=input_path,
                            output_path=output_path,
                            config_path=config_path,
                            source_colorspace=source_colorspace,
                            target_colorspace=target_colorspace,
                            target_display=target_display,
                            target_view=target_view,
                            source_display=source_display,
                            source_view=source_view,
                            additional_command_args=additional_command_args,
                            frames=frames,
                            frame_padding=frame_padding,
                            parallel_frames=parallel_frames,
                            review_layers=review_layers,
                            logger=self.log,
                        )
                    except MissingRGBAChannelsError as exc:
                        missing_rgba_review_channels = True
                        self.log.error(exc)
                        self.log.error(
                            "Skipping OIIO Transcode. Unknown RGBA channels"
                            f" for colorspace conversion in file: {input_path}"
                        )
                        break

                if missing_rgba_review_channels:
                    # Stop processing this representation
                    break

                # cleanup temporary transcoded files
                for file_name in new_repre["files"]:
                    transcoded_file_path = os.path.join(new_staging_dir,
                                                        file_name)
                    instance.context.data["cleanupFullPaths"].append(
                        transcoded_file_path)

                custom_tags = output_def.get("custom_tags")
                if custom_tags:
                    if new_repre.get("custom_tags") is None:
                        new_repre["custom_tags"] = []
                    new_repre["custom_tags"].extend(custom_tags)

                # Add additional tags from output definition to representation
                if new_repre.get("tags") is None:
                    new_repre["tags"] = []
                for tag in output_def["tags"]:
                    if tag not in new_repre["tags"]:
                        new_repre["tags"].append(tag)

                    if tag == "review":
                        added_review = True

                # If there is only 1 file outputted then convert list to
                # string, because that'll indicate that it is not a sequence.
                if len(new_repre["files"]) == 1:
                    new_repre["files"] = new_repre["files"][0]

                # If the source representation has "review" tag, but it's not
                # part of the output definition tags, then both the
                # representations will be transcoded in ExtractReview and
                # their outputs will clash in integration.
                if "review" in repre.get("tags", []):
                    added_review = True

                new_representations.append(new_repre)
                added_representations = True

            if added_representations:
                self._mark_original_repre_for_deletion(
                    repre, profile, added_review
                )

            tags = repre.get("tags") or []
            if "delete" in tags and "thumbnail" not in tags:
                instance.data["representations"].remove(repre)

            # In case instance is not flagged for reviewable workflow
            # by `review` family we have to add it so it can be processed
            # by ExtractReview plugin
            if (
                added_review
                and "review" not in instance.data["families"]
            ):
                # TODO: Preferably we do not mess with families
                #  at this point in processing, but ExtractReview
                #  currently requires it. And this is the only way
                #  to have a representation with `review` tag
                #  actually getting picked up for non-review
                #  families.
                instance.data["families"].append("review")

        instance.data["representations"].extend(new_representations)

    def _rename_in_representation(self, new_repre, files_to_convert,
                                  output_name, output_extension):
        """Replace old extension with new one everywhere in representation.

        Args:
            new_repre (dict)
            files_to_convert (list): of filenames from repre["files"],
                standardized to always list
            output_name (str): key of output definition from Settings,
                if "<passthrough>" token used, keep original repre name
            output_extension (str): extension from output definition
        """
        if output_name != "passthrough":
            new_repre["name"] = output_name
        if not output_extension:
            return

        new_repre["ext"] = output_extension
        new_repre["outputName"] = output_name

        renamed_files = []
        for file_name in files_to_convert:
            file_name, _ = os.path.splitext(file_name)
            file_name = '{}.{}'.format(file_name,
                                       output_extension)
            renamed_files.append(file_name)
        new_repre["files"] = renamed_files

    def _translate_to_sequence(self, files_to_convert):
        """Returns original individual filepaths or list of clique.Collection.

        Uses clique to find frame sequence, and return the collections instead.
        If sequence not detected in input filenames, it returns original list.

        Args:
            files_to_convert (list[str]): list of file names
        Returns:
            list[str | clique.Collection]: List of
                filepaths ['fileA.exr', 'fileB.exr']
                or clique.Collection for a sequence.

        """
        pattern = [clique.PATTERNS["frames"]]
        collections, _ = clique.assemble(
            files_to_convert, patterns=pattern,
            assume_padded_when_ambiguous=True)
        if collections:
            if len(collections) > 1:
                raise ValueError(
                    "Too many collections {}".format(collections))

            return collections

        return files_to_convert

    def _get_output_file_path(self, input_path, output_dir,
                              output_extension):
        """Create output file name path."""
        file_name = os.path.basename(input_path)
        file_name, input_extension = os.path.splitext(file_name)
        if not output_extension:
            output_extension = input_extension.replace(".", "")
        new_file_name = '{}.{}'.format(file_name,
                                       output_extension)
        return os.path.join(output_dir, new_file_name)

    def _get_profile(self, instance):
        """Returns profile if and how repre should be color transcoded."""
        host_name = instance.context.data["hostName"]
        product_base_type = instance.data.get("productBaseType")
        if not product_base_type:
            product_base_type = instance.data["productType"]
        product_name = instance.data["productName"]
        task_data = instance.data["anatomyData"].get("task", {})
        task_name = task_data.get("name")
        task_type = task_data.get("type")
        filtering_criteria = {
            "host_names": host_name,
            "product_base_types": product_base_type,
            "product_names": product_name,
            "task_names": task_name,
            "task_types": task_type,
        }
        profile = filter_profiles(
            self.profiles,
            filtering_criteria,
            logger=self.log
        )

        if not profile:
            self.log.debug(
                "Skipped instance. None of profiles in presets are for"
                f" Host name: \"{host_name}\""
                f" | Product base type: \"{product_base_type}\""
                f" | Product name: \"{product_name}\""
                f" | Task name \"{task_name}\""
                f" | Task type \"{task_type}\""
            )

        return profile

    def _repre_is_valid(self, repre, profile):
        """Validation if representation should be processed.

        Args:
            repre (dict): Representation which should be checked.

        Returns:
            bool: False if can't be processed else True.
        """

        if repre.get("ext") not in self.supported_exts:
            self.log.debug((
                "Representation '{}' has unsupported extension: '{}'. Skipped."
            ).format(repre["name"], repre.get("ext")))
            return False

        if not repre.get("files"):
            self.log.debug((
                "Representation '{}' has empty files. Skipped."
            ).format(repre["name"]))
            return False

        if not repre.get("colorspaceData"):
            self.log.debug("Representation '{}' has no colorspace data. "
                           "Skipped.".format(repre["name"]))
            return False

        representations_names = profile["representation_names"]

        # make sure that positive will be returned if no representations_names
        if not representations_names:
            return True

        repre_name = repre["name"]

        # check if any of representation patterns match in repre_name
        for r_pattern in representations_names:
            if re.match(r_pattern, repre_name):
                return True

        return False

    def _mark_original_repre_for_deletion(self, repre, profile, added_review):
        """If new transcoded representation created, delete old."""
        if not repre.get("tags"):
            repre["tags"] = []

        delete_original = profile["delete_original"]

        if delete_original:
            if "delete" not in repre["tags"]:
                repre["tags"].append("delete")

        if added_review and "review" in repre["tags"]:
            repre["tags"].remove("review")
