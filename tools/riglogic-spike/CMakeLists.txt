# RigLogicLib, built standalone — slice 5.0 of the face-fidelity plan (F5).
#
# Epic ships RigLogicLib as a UBT module (RigLogicLib.Build.cs), not a CMake project, so this
# file exists to build the *unmodified* engine source outside Unreal. Nothing here is a
# reimplementation: the whole point of the spike is that the evaluator is Epic's, byte for
# byte, and the only thing being proven is that it builds and runs away from the engine.
#
# Point RIGLOGIC_SOURCE_DIR at the plugin's RigLogicLib directory:
#
#   cmake -B build -S . -DRIGLOGIC_SOURCE_DIR="C:/Program Files/Epic Games/UE_5.8/Engine/Plugins/Animation/RigLogic/Source/RigLogicLib"
#   cmake --build build --config Release
#
# The same CMakeLists is reused for 5.1 under emcmake, which is why nothing below is
# MSVC-specific and why the platform defines are set by inspection rather than copied
# wholesale from the .Build.cs.

cmake_minimum_required(VERSION 3.20)
project(riglogic_spike CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT DEFINED RIGLOGIC_SOURCE_DIR)
  message(FATAL_ERROR "Set -DRIGLOGIC_SOURCE_DIR=<path to>/RigLogic/Source/RigLogicLib")
endif()
if(NOT EXISTS "${RIGLOGIC_SOURCE_DIR}/Public/riglogic/riglogic/RigLogic.h")
  message(FATAL_ERROR "RIGLOGIC_SOURCE_DIR does not look like RigLogicLib: ${RIGLOGIC_SOURCE_DIR}")
endif()

# The library is a flat set of translation units under Private/. Globbing is normally a poor
# habit, but here the file list belongs to Epic and is expected to change between engine
# versions — a hand-maintained list would be a copy that silently drifts, which is the exact
# failure mode this spike is meant to avoid.
file(GLOB_RECURSE RIGLOGIC_SOURCES CONFIGURE_DEPENDS "${RIGLOGIC_SOURCE_DIR}/Private/*.cpp")

# Exactly two translation units in the module are Unreal *glue* rather than RigLogic, and they
# are the only things in the tree that include CoreMinimal.h:
#
#   RigLogicLib.cpp          the IModuleInterface implementation — module startup/shutdown
#   ArchiveMemoryStream.cpp  an adapter presenting UE's FArchive as a trio::BoundedIOStream
#
# (Public/FMemoryResource.h is a third, a UE allocator adapter, but it is header-only and no
# translation unit here includes it.)
#
# Dropping them is scoping, not patching: the library core — dna, riglogic, pma, trio, terse,
# status, tdm, trimd — is engine-independent by construction, which is why Epic also ships it
# standalone as dnacalib. Nothing excluded here participates in evaluation, and the spike would
# be worthless if it did, since the point is that the evaluator is Epic's unmodified code.
#
# Listed by name rather than filtered by a grep for CoreMinimal.h so that a future engine
# version adding a third glue file fails the build loudly instead of being silently dropped.
set(RIGLOGIC_UE_GLUE
  "${RIGLOGIC_SOURCE_DIR}/Private/RigLogicLib.cpp"
  "${RIGLOGIC_SOURCE_DIR}/Private/ArchiveMemoryStream.cpp")
foreach(glue ${RIGLOGIC_UE_GLUE})
  if(NOT EXISTS "${glue}")
    message(FATAL_ERROR "Expected UE glue file is missing: ${glue}")
  endif()
  list(REMOVE_ITEM RIGLOGIC_SOURCES "${glue}")
endforeach()

list(LENGTH RIGLOGIC_SOURCES RIGLOGIC_SOURCE_COUNT)
message(STATUS "RigLogicLib translation units: ${RIGLOGIC_SOURCE_COUNT} (2 UE glue files excluded)")

add_library(riglogic STATIC ${RIGLOGIC_SOURCES})

target_include_directories(riglogic PUBLIC
  "${RIGLOGIC_SOURCE_DIR}/Public"
  "${RIGLOGIC_SOURCE_DIR}/Private")

# RL_BUILD_WITH_XYZ_ROTATION_ORDER is PUBLIC in the .Build.cs, not private: it changes the
# meaning of the rotation values in getJointOutputs(), so a consumer built without it would
# read Epic's numbers through the wrong convention and see a plausibly-wrong face. It is set
# here for both the library and the harness for that reason.
target_compile_definitions(riglogic PUBLIC RL_BUILD_WITH_XYZ_ROTATION_ORDER)

# Static build: RL_BUILD_SHARED / RL_SHARED are deliberately NOT set, so RLAPI resolves to
# nothing and no dllexport is attempted.
# Storage/vectorisation path. Epic's Win64 build sets all three, but they are exposed as
# options here because they decide the in-memory layout of the optimised rig, and a mismatch
# between how it is written and how it is read presents as correct counts with garbage floats
# rather than as a build error. Which combination is correct outside UBT is an empirical
# question, so it is answered empirically.
option(RIGLOGIC_AUTODETECT_SSE "Runtime-detect SSE" ON)
# Autodetect is meaningless without a CPU to detect; forced off for wasm.
if(EMSCRIPTEN)
  set(RIGLOGIC_AUTODETECT_SSE OFF)
  set(RIGLOGIC_AUTODETECT_HALF_FLOATS OFF)
  set(RIGLOGIC_HALF_FLOATS OFF)
endif()
option(RIGLOGIC_AUTODETECT_HALF_FLOATS "Runtime-detect F16C" ON)
option(RIGLOGIC_HALF_FLOATS "Compile the half-float storage path" ON)
if(RIGLOGIC_AUTODETECT_SSE)
  target_compile_definitions(riglogic PRIVATE RL_AUTODETECT_SSE)
endif()
if(RIGLOGIC_AUTODETECT_HALF_FLOATS)
  target_compile_definitions(riglogic PRIVATE RL_AUTODETECT_HALF_FLOATS)
endif()

if(EMSCRIPTEN)
  # RL_DISABLE_RUNTIME_FEATURE_DETECTION is REQUIRED here, not an optimisation choice.
  #
  # riglogic/system/simd/SIMD.h turns on TRIMD_ENABLE_RUNTIME_FEATURE_DETECTION unless this is
  # defined, and trimd/Platform.h only supplies getCPUFeatures() inside that branch for
  # TRIMD_PLATFORM_X86 or TRIMD_PLATFORM_ARM. wasm is neither, so the declaration simply does
  # not exist and seven translation units fail with "no member named 'getCPUFeatures'".
  # Defining this selects Platform.h's #else branch, whose getCPUFeatures() returns an empty
  # feature set -- which is the honest answer for a target that cannot interrogate a CPU.
  #
  # The consequence is the scalar evaluation path, and that is the correct baseline for 5.1's
  # first number. A SIMD128 build is a separate measurement, and reporting one while calling
  # it the other is exactly the kind of quiet substitution this programme keeps paying for.
  target_compile_definitions(riglogic PRIVATE
    RL_DISABLE_RUNTIME_FEATURE_DETECTION
    TRIMD_DISABLE_SSE
    TRIMD_DISABLE_AVX)
elseif(WIN32)
  # The .Build.cs points TRIMD/TRIO at "WindowsPlatformUE.h". That header is Unreal's, not
  # the library's, so outside the engine the custom-header defines are left unset and the
  # sources include <windows.h> directly.
  # Epic's Win64 private define set, copied from RigLogicLib.Build.cs rather than summarised.
  # RL_BUILD_WITH_HALF_FLOATS is a storage-layout define -- omitting it while the autodetect
  # defines are present is not a missing optimisation, it is two halves of the build
  # disagreeing about the size of things, which presents as heap corruption at load.
  #
  # TRIMD_CUSTOM_WINDOWS_H / TRIO_CUSTOM_WINDOWS_H are deliberately NOT copied: they point at
  # "WindowsPlatformUE.h", which is Unreal's header, not the library's. Left unset, the sources
  # include <windows.h> directly, which is what the standalone build wants.
  target_compile_definitions(riglogic PRIVATE
    TRIO_WINDOWS_FILE_MAPPING_AVAILABLE
    NOMINMAX
    WIN32_LEAN_AND_MEAN)
  if(RIGLOGIC_HALF_FLOATS)
    target_compile_definitions(riglogic PRIVATE RL_BUILD_WITH_HALF_FLOATS)
  endif()
  if(MSVC)
    target_compile_options(riglogic PRIVATE /bigobj)
  endif()
else()
  target_compile_definitions(riglogic PRIVATE TRIO_MMAP_AVAILABLE)
endif()

add_executable(riglogic_probe probe.cpp)
target_link_libraries(riglogic_probe PRIVATE riglogic)

if(EMSCRIPTEN)
  # Node under emscripten needs the host filesystem mounted to read a .dna from disk, and
  # 4 MB of behaviour layer plus RigLogic's working set needs the heap to grow.
  set_target_properties(riglogic_probe PROPERTIES
    LINK_FLAGS "-sNODERAWFS=1 -sALLOW_MEMORY_GROWTH=1 -sEXIT_RUNTIME=1 -O2")

  # ---- the library the browser worker loads (slice 5.2b) ------------------------------
  #
  # Separate target from the probe on purpose. The probe is a command-line harness with a
  # filesystem and an exit code; the worker gets no filesystem, never exits, and receives its
  # DNA as bytes over postMessage. Sharing one target would drag NODERAWFS into the browser
  # build and give the page a filesystem shim it has no use for.
  #
  # MODULARIZE + EXPORT_ES6 so the worker can `import` it rather than relying on a global,
  # and ENVIRONMENT=worker so emscripten does not emit node or shell branches the bundler
  # would then have to reason about.
  add_executable(riglogic_wasm riglogic_wasm.cpp)
  target_link_libraries(riglogic_wasm PRIVATE riglogic)
  set_target_properties(riglogic_wasm PROPERTIES
    OUTPUT_NAME "riglogic"
    SUFFIX ".mjs"
    LINK_FLAGS
      "-O3 -sMODULARIZE=1 -sEXPORT_ES6=1 -sENVIRONMENT=worker,web \
       -sALLOW_MEMORY_GROWTH=1 -sEXPORTED_RUNTIME_METHODS=['ccall','cwrap','HEAPF32','HEAPU8'] \
       -sEXPORTED_FUNCTIONS=['_malloc','_free','_rl_init','_rl_raw_control_count',\
'_rl_joint_output_count','_rl_blend_shape_count','_rl_lod_count','_rl_set_lod',\
'_rl_set_raw_control','_rl_set_raw_controls','_rl_calculate','_rl_joint_outputs_ptr',\
'_rl_blend_shape_outputs_ptr','_rl_shutdown']")
endif()
