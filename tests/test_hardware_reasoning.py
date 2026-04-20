from __future__ import annotations

from quickhatch.analysis.distro_engine import recommend
from quickhatch.analysis.driver_check import check_drivers
from quickhatch.config import GPUInfo, HardwareInfo, Preferences, UserProfile


class TestDriverCheck:
    def test_duplicate_nvidia_gpus_produce_one_driver_recommendation(self):
        hw = HardwareInfo(
            gpus=[
                GPUInfo(name="RTX 3080", vendor="nvidia"),
                GPUInfo(name="RTX 3070", vendor="nvidia"),
            ]
        )
        recs = check_drivers(hw)
        nvidia = [r for r in recs if r["component"] == "GPU: NVIDIA"]
        assert len(nvidia) == 1

    def test_hybrid_graphics_produce_one_recommendation_per_vendor(self):
        hw = HardwareInfo(
            gpus=[
                GPUInfo(name="RTX 3080", vendor="nvidia"),
                GPUInfo(name="Intel UHD Graphics", vendor="intel"),
                GPUInfo(name="RTX 3070", vendor="nvidia"),
            ]
        )
        recs = check_drivers(hw)
        components = {r["component"] for r in recs}
        assert "GPU: NVIDIA" in components
        assert "GPU: Intel" in components
        assert len([r for r in recs if r["component"] == "GPU: NVIDIA"]) == 1


class TestDistroRecommend:
    def test_duplicate_nvidia_gpus_do_not_double_count_nvidia_bonus(self):
        base = UserProfile(
            preferences=Preferences(migration_type="fresh_install"),
            hardware=HardwareInfo(gpus=[GPUInfo(name="RTX 3080", vendor="nvidia")]),
        )
        duplicate = UserProfile(
            preferences=Preferences(migration_type="fresh_install"),
            hardware=HardwareInfo(
                gpus=[
                    GPUInfo(name="RTX 3080", vendor="nvidia"),
                    GPUInfo(name="RTX 3070", vendor="nvidia"),
                ]
            ),
        )

        base_scores = dict((name, score) for name, score, _reason in recommend(base))
        duplicate_scores = dict((name, score) for name, score, _reason in recommend(duplicate))
        assert base_scores["Pop!_OS"] == duplicate_scores["Pop!_OS"]
        assert base_scores["Ubuntu"] == duplicate_scores["Ubuntu"]
