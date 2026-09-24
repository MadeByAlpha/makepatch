from hatchling.plugin import hookimpl

from makepatch.hatch.hook import MakepatchBuildHook


@hookimpl
def hatch_register_build_hook():
    return MakepatchBuildHook
