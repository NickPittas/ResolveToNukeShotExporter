import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).parents[1] / "ResolveToNukeShotExporter.py"
SPEC = importlib.util.spec_from_file_location("rtn", MODULE)
rtn = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = rtn
SPEC.loader.exec_module(rtn)


def example_shot(timecode="01:00:00:00"):
    primary = rtn.ItemRef(1, "V1", 1, "A001", 100, 110, "/show/a001.exr", "a", timecode, 10, 20, 2048, 1152)
    v2 = rtn.ItemRef(2, "V2", 1, "FG", 102, 108, "/show/fg.exr", "b", "02:00:00:00", 5, 5, 2048, 1152)
    return rtn.Shot(1, "seq_001", primary, [primary, v2])


class TimelineItem:
    def __init__(self, name, start, end, path):
        self.name, self.start, self.end, self.path = name, start, end, path
        self.renamed_to = None

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetMediaPoolItem(self):
        return self

    def GetClipProperty(self):
        return {"File Path": self.path}

    def GetLeftOffset(self):
        return 0

    def GetRightOffset(self):
        return 0

    def GetMediaId(self):
        return self.name

    def SetName(self, name):
        self.renamed_to = name
        return True


class Timeline:
    def __init__(self, tracks):
        self.tracks = tracks

    def GetTrackCount(self, kind):
        return len(self.tracks)

    def GetTrackName(self, kind, index):
        return "V%d" % index

    def GetItemListInTrack(self, kind, index):
        return self.tracks[index - 1]


class CoreTests(unittest.TestCase):
    def test_tokens_and_padding(self):
        result = rtn.expand_tokens("{sequence}/{shot_index:03}/{frame}.{ext}", {"sequence": "seq 01", "shot_index": 7, "frame": "####", "ext": "exr"})
        self.assertEqual(result, "seq_01/007/####.exr")

    def test_unknown_token_is_rejected(self):
        with self.assertRaises(rtn.ExportError):
            rtn.expand_tokens("{nope}", {})

    def test_platform_script_module_locations(self):
        paths = rtn._resolve_scripting_module_paths()
        self.assertTrue(paths)
        self.assertTrue(all(isinstance(path, Path) for path in paths))

    def test_documented_scaling_choices_are_exposed(self):
        self.assertEqual(set(rtn.SCALING_METHODS), {"Use Project", "Crop", "Fit", "Fill", "Stretch"})
        self.assertEqual(rtn.RESIZE_FILTER_VALUES["Use Project"], 0)

    def test_timeline_rename_requires_resolve_20_2(self):
        class Resolve20_0:
            def GetVersion(self):
                return [20, 0, 0, 23]
        class Resolve20_2:
            def GetVersion(self):
                return [20, 2, 0, 1]
        self.assertFalse(rtn.supports_timeline_item_rename(Resolve20_0()))
        self.assertTrue(rtn.supports_timeline_item_rename(Resolve20_2()))

    def test_render_options_use_resolve_format_id_for_codecs(self):
        class Project:
            def GetRenderFormats(self):
                return {"QuickTime": "mov", "EXR": "exr"}
            def GetRenderCodecs(self, format_id):
                return {"Apple ProRes 4444": "ProRes4444"} if format_id == "mov" else {"RGB half": "RGBHalf"}
        options = rtn.ResolveExporter(None, Project(), None).render_options()
        self.assertEqual(options["QuickTime"]["format"], "mov")
        self.assertEqual(options["QuickTime"]["codecs"]["Apple ProRes 4444"], "ProRes4444")

    def test_source_range_clamps_handles(self):
        shot = example_shot()
        self.assertEqual(rtn.source_range(shot, shot.primary, 20), (90, 129))
        self.assertEqual(rtn.source_range(shot, shot.sources[1], 20), (97, 112))

    def test_timecode_uses_selected_track_then_primary_fallback(self):
        shot = example_shot()
        settings = rtn.ExportSettings("/show", "seq", handles=0, timecode_track=2)
        self.assertEqual(rtn.timecode_for_shot(shot, settings, 24), "02:00:00:05")
        settings.timecode_track = 3
        self.assertEqual(rtn.timecode_for_shot(shot, settings, 24), "01:00:00:10")

    def test_empty_v1_uses_next_non_empty_track_and_all_sources(self):
        primary = TimelineItem("BG", 100, 110, "/show/bg.exr")
        layer = TimelineItem("FG", 102, 108, "/show/fg.exr")
        shots = rtn.inspect_timeline(Timeline([[], [primary], [layer]]), [])
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0].primary.track_index, 2)
        self.assertEqual([source.track_index for source in shots[0].sources], [2, 3])

    def test_primary_track_can_be_renamed_when_v1_is_empty(self):
        primary = TimelineItem("BG", 100, 110, "/show/bg.exr")
        shots = rtn.inspect_timeline(Timeline([[], [primary]]), [])
        settings = rtn.ExportSettings("/show", "seq", shot_template="{sequence}_{shot_index:03}")
        self.assertEqual(rtn.rename_shots(shots, settings), ["seq_001"])
        self.assertEqual(primary.renamed_to, "seq_001")

    def test_empty_v1_uses_primary_track_as_nuke_write_input(self):
        primary = TimelineItem("BG", 100, 110, "/show/bg.exr")
        layer = TimelineItem("FG", 102, 108, "/show/fg.exr")
        shot = rtn.inspect_timeline(Timeline([[], [primary], [layer]]), [])[0]
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(temp, "seq")
            plan = rtn.build_plan([shot], settings, 24)[0]
            rtn.write_nuke_script(plan, settings, {})
            self.assertIn("push $Read_V2_BG_01", plan.script_path.read_text())

    def test_movie_outputs_remove_frame_token_and_filename_separator(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(
                temp, "seq", write_file_type="mov", write_extension="mov",
                nuke_write_codec="H.264",
                plate_format="mov", plate_extension="mov", export_plates=True,
                script_template="Scripts/{shot}_{frame}.nk",
                write_template="Renders/{shot}_{version}_{frame}.{ext}",
                plate_template="Plates/{shot}_{track}_{frame}.{ext}",
            )
            plan = rtn.build_plan([example_shot()], settings, 24)[0]
            self.assertEqual(plan.script_path.name, "seq_001.nk")
            self.assertEqual(plan.write_path.name, "seq_001_v001.mov")
            self.assertEqual(plan.sources[0].path.name, "seq_001_V1.mov")
            self.assertNotIn("#", str(plan.write_path))
            self.assertNotIn("#", str(plan.sources[0].path))
            rtn.write_nuke_script(plan, settings, {})
            body = plan.script_path.read_text()
            self.assertIn('mov64_codec "H.264"', body)
            self.assertIn(" first 1", body)
            self.assertIn(" last 10", body)

    def test_image_sequence_outputs_keep_frame_token(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(temp, "seq", export_plates=True)
            plan = rtn.build_plan([example_shot()], settings, 24)[0]
            self.assertEqual(plan.write_path.name, "seq_001_comp_v001_####.exr")
            self.assertEqual(plan.sources[0].path.name, "seq_001_V1_A001_v001.####.exr")

    def test_rename_all_tracks_warns_for_duplicates_but_applies_names(self):
        first = TimelineItem("BG", 100, 110, "/show/bg.exr")
        second = TimelineItem("FG", 100, 110, "/show/fg.exr")
        tracks = rtn.inspect_video_tracks(Timeline([[first], [second]]))
        settings = rtn.ExportSettings("/show", "seq", shot_template="{sequence}_{shot_index:03}")
        preview, warnings = rtn.rename_timeline_items_preview(tracks, settings, [])
        self.assertEqual(preview, ["V1  001  seq_001", "V2  001  seq_001"])
        self.assertEqual(warnings, ["seq_001"])
        renamed, warnings = rtn.rename_timeline_items(tracks, settings, [])
        self.assertEqual(renamed, ["V1: seq_001", "V2: seq_001"])
        self.assertEqual(warnings, ["seq_001"])
        self.assertEqual(first.renamed_to, "seq_001")
        self.assertEqual(second.renamed_to, "seq_001")

    def test_movie_plate_uses_native_nuke_frame_range_not_timeline_frames(self):
        primary = rtn.ItemRef(1, "V1", 1, "Movie", 90000, 90201, "/show/movie.mov")
        shot = rtn.Shot(1, "seq_001", primary, [primary])
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(temp, "seq", export_plates=True, plate_extension="mov", plate_format="mov")
            plan = rtn.build_plan([shot], settings, 24)[0]
            rtn.write_nuke_script(plan, settings, {})
            body = plan.script_path.read_text()
            self.assertIn(" first 1", body)
            self.assertIn(" last 201", body)
            self.assertIn(" origfirst 1", body)
            self.assertIn(" origlast 201", body)
            self.assertNotIn(" first 90000", body)
            self.assertIn("Root {\n first_frame 1001\n last_frame 1201", body)
            self.assertIn("TimeClip {\n inputs 1\n first 1\n last 201\n frame_mode \"start at\"\n frame 1001", body)

    def test_original_movie_read_uses_the_timeline_clip_source_in_out(self):
        source = rtn.ItemRef(
            1, "V1", 1, "Cut_B", 90000, 90519, "/show/one_hour_movie.mov",
            left_offset=249, right_offset=3119,
        )
        shot = rtn.Shot(1, "cut_b", source, [source])
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(temp, "seq", export_plates=False, nuke_start_frame=1001)
            plan = rtn.build_plan([shot], settings, 24)[0]
            rtn.write_nuke_script(plan, settings, {})
            body = plan.script_path.read_text()
            self.assertIn('file "/show/one_hour_movie.mov"\n first 1\n last 3887', body)
            self.assertIn("TimeClip {\n inputs 1\n first 250\n last 768\n frame_mode \"start at\"\n frame 1001", body)
            self.assertIn("Root {\n first_frame 1001\n last_frame 1519", body)

    def test_add_timecode_uses_documented_knobs_and_native_start_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(temp, "seq", export_plates=True, plate_extension="mov", plate_format="mov")
            plan = rtn.build_plan([example_shot()], settings, 24)[0]
            rtn.write_nuke_script(plan, settings, {})
            body = plan.script_path.read_text()
            self.assertIn('startcode "01:00:00:10"', body)
            self.assertIn(" useFrame true", body)
            self.assertIn(" frame 1001", body)
            self.assertNotIn(" timecode ", body)

    def test_nuke_start_frame_offsets_image_sequences_and_root_range(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(temp, "seq", nuke_start_frame=1100)
            plan = rtn.build_plan([example_shot()], settings, 24)[0]
            rtn.write_nuke_script(plan, settings, {})
            body = plan.script_path.read_text()
            self.assertIn("Root {\n first_frame 1100\n last_frame 1109", body)
            self.assertIn("Read {\n file \"/show/a001.exr\"\n first 100\n last 109", body)
            self.assertIn("TimeClip {\n inputs 1\n first 100\n last 109\n frame_mode \"start at\"\n frame 1100", body)

    def test_drop_frame_timecode(self):
        self.assertEqual(rtn.add_frames_to_timecode("01:00:59;29", 1, 29.97), "01:01:00;02")

    def test_missing_timecode_omits_add_node(self):
        with tempfile.TemporaryDirectory() as temp:
            shot = example_shot("")
            settings = rtn.ExportSettings(temp, "seq", selected_tracks=[1], version="v001")
            plan = rtn.build_plan([shot], settings, 24)[0]
            rtn.write_nuke_script(plan, settings, {})
            body = plan.script_path.read_text()
            self.assertNotIn("AddTimeCode {", body)
            self.assertIn("intentionally omitted", body)

    def test_preflight_blocks_existing_scripts_and_plate_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            shot = example_shot()
            settings = rtn.ExportSettings(temp, "seq", selected_tracks=[1], export_plates=True)
            plan = rtn.build_plan([shot], settings, 24)[0]
            plan.script_path.parent.mkdir(parents=True)
            plan.script_path.write_text("already here")
            errors = rtn.preflight([plan])
            self.assertTrue(any("Existing Nuke script" in item for item in errors))

    def test_plate_track_selection_keeps_other_tracks_as_original_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(temp, "seq", selected_tracks=[1, 2], export_plates=True, plate_tracks=[2])
            plan = rtn.build_plan([example_shot()], settings, 24)[0]
            self.assertFalse(plan.sources[0].use_plate)
            self.assertTrue(plan.sources[1].use_plate)

    def test_nuke_script_and_manifest_include_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            shot = example_shot()
            settings = rtn.ExportSettings(temp, "seq", selected_tracks=[1, 2], sidecar_manifest=True)
            plan = rtn.build_plan([shot], settings, 24)[0]
            rtn.write_nuke_script(plan, settings, {"colorScienceMode": "davinciYRGBColorManaged"})
            rtn.write_manifest(plan, settings, {"colorScienceMode": "davinciYRGBColorManaged"})
            body = plan.script_path.read_text()
            self.assertIn("Read_V1_A001", body)
            self.assertIn("Read_V2_FG", body)
            self.assertIn("AddTimeCode {", body)
            self.assertIn("push $Read_V1_A001_01", body)
            manifest = json.loads(plan.manifest_path.read_text())
            self.assertEqual(manifest["shot"]["name"], "seq_001")
            self.assertEqual(len(manifest["sources"]), 2)

    def test_script_writer_refuses_a_second_write(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = rtn.ExportSettings(temp, "seq", selected_tracks=[1])
            plan = rtn.build_plan([example_shot()], settings, 24)[0]
            rtn.write_nuke_script(plan, settings, {})
            with self.assertRaises(rtn.ExportError):
                rtn.write_nuke_script(plan, settings, {})


if __name__ == "__main__":
    unittest.main()
