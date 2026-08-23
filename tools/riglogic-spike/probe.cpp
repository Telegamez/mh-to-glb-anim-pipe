// Slice 5.0 — prove Epic's RigLogicLib builds outside Unreal, loads a .dna, and evaluates.
//
// This is the whole point of the spike, and it is deliberately small. The evaluator is
// Epic's, unmodified; nothing here reimplements any part of it. What this file establishes
// is that the API is what we think it is and that the numbers come out of the real thing.
//
//   riglogic_probe <path to .dna> [--frames N] [--lod L] [--json out.json]
//
// It prints the rig's declared shape, evaluates one frame from a known control vector, and
// reports actual joint / blendshape / animated-map outputs. With --frames it also times the
// steady-state evaluation, which is the measurement slice 5.1 repeats under emscripten so the
// two numbers are directly comparable — same source, same DNA, same control stream.

#include <dna/BinaryStreamReader.h>
#include <dna/Configuration.h>
#include <riglogic/RigLogic.h>
#include <trio/Stream.h>
#include <trio/streams/FileStream.h>

#if defined(_WIN32)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif
#if defined(_MSC_VER)
#include <crtdbg.h>
#include <cstdlib>
#endif

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <string>
#include <vector>

namespace {

struct Args {
    const char* dna = nullptr;
    const char* json = nullptr;
    const char* controls = nullptr;
    const char* outputs = nullptr;
    int frames = 0;
    int lod = 0;
};

Args parseArgs(int argc, char** argv) {
    Args a;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--frames") == 0 && i + 1 < argc) {
            a.frames = std::atoi(argv[++i]);
        } else if (std::strcmp(argv[i], "--lod") == 0 && i + 1 < argc) {
            a.lod = std::atoi(argv[++i]);
        } else if (std::strcmp(argv[i], "--json") == 0 && i + 1 < argc) {
            a.json = argv[++i];
        } else if (std::strcmp(argv[i], "--controls") == 0 && i + 1 < argc) {
            a.controls = argv[++i];
        } else if (std::strcmp(argv[i], "--outputs") == 0 && i + 1 < argc) {
            a.outputs = argv[++i];
        } else if (a.dna == nullptr) {
            a.dna = argv[i];
        }
    }
    return a;
}

// A deterministic, reproducible control stream. Not random: the same values must come out of
// the native and the wasm build for their outputs to be comparable at all, and "the two
// builds agree" is the only evidence that the wasm one is the same evaluator.
float controlValue(std::uint16_t index, int frame) {
    const float phase = static_cast<float>(index) * 0.37f + static_cast<float>(frame) * 0.021f;
    return 0.5f * (1.0f + std::sin(phase));
}

// A Debug-CRT abort() or failed assertion opens a modal dialog by default. This tool is run
// from a build script and, more to the point, on someone else's desktop -- a blocking dialog
// is not a diagnostic, it is an interruption, and it hides the very message worth reading.
// Everything is routed to stderr and the process is allowed to die.
void noInteractiveFailures() {
#if defined(_WIN32)
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
#endif
#if defined(_MSC_VER) && defined(_DEBUG)
    _set_abort_behavior(0, _WRITE_ABORT_MSG | _CALL_REPORTFAULT);
    for (int report : {_CRT_WARN, _CRT_ERROR, _CRT_ASSERT}) {
        _CrtSetReportMode(report, _CRTDBG_MODE_FILE);
        _CrtSetReportFile(report, _CRTDBG_FILE_STDERR);
    }
#endif
}

double checksum(const rl4::ConstArrayView<float>& v) {
    double sum = 0.0;
    for (std::size_t i = 0; i < v.size(); ++i) {
        sum += static_cast<double>(v[i]) * static_cast<double>(i % 97 + 1);
    }
    return sum;
}

}  // namespace

int main(int argc, char** argv) {
    // Unbuffered: a crash inside the library must not swallow the progress markers that say
    // how far it got. Diagnosing from "no output at all" is diagnosing from nothing.
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    noInteractiveFailures();

    const Args args = parseArgs(argc, argv);
    if (args.dna == nullptr) {
        std::fprintf(stderr, "usage: riglogic_probe <file.dna> [--frames N] [--lod L] [--json out]\n");
        return 2;
    }

    // Everything EXCEPT Geometry.
    //
    // The first version loaded DataLayer::Behavior alone, on the reasoning that behaviour is
    // all an evaluator needs. It is not, and the failure was instructive: RigLogic asserted
    // `index < sz` inside ArrayView during calculate(). The joint matrix's input indices run
    // up to 825, while raw controls (263) plus PSDs (545) account for only 808 — the extra 18
    // are RBF and machine-learned solver outputs, which live in rbfb/rbfe/mlbh and were never
    // loaded, so the control buffer was 18 slots shorter than the matrix indexes into.
    //
    // Independently corroborated: the engine-free parse in tools/dna/parse_bhvr.py found the
    // same out-of-range indices and labelled them "solver outputs" before this ever ran.
    //
    // Geometry stays out, and only Geometry: geom is ~91% of the file and an evaluator never
    // reads it, because the GLB already carries the meshes.
    const auto layers = dna::DataLayer::RBFBehavior             // implies Behavior
                        | dna::DataLayer::MachineLearnedBehavior
                        | dna::DataLayer::JointBehaviorMetadata
                        | dna::DataLayer::TwistSwingBehavior;
    auto* stream = trio::FileStream::create(args.dna,
                                            trio::FileStream::AccessMode::Read,
                                            trio::FileStream::OpenMode::Binary);
    if (stream == nullptr) {
        std::fprintf(stderr, "could not open %s\n", args.dna);
        return 1;
    }

    // Do NOT call stream->open() here: BinaryStreamReader::read() opens the stream itself,
    // and an explicit open first makes read() fail with "File already open". The reader owns
    // the stream's open/close lifecycle; the caller owns only its destruction. Established by
    // trying it, not assumed.
    std::printf("  [stream created]\n");
    const auto t_open = std::chrono::steady_clock::now();
    auto* reader = dna::BinaryStreamReader::create(stream, layers);
    std::printf("  [reader created]\n");
    reader->read();
    std::printf("  [read complete]\n");
    if (!dna::Status::isOk()) {
        std::fprintf(stderr, "DNA read failed: %s\n", dna::Status::get().message);
        return 1;
    }
    const auto t_read = std::chrono::steady_clock::now();

    std::printf("  [status ok]\n");
    auto* rigLogic = rl4::RigLogic::create(reader);
    std::printf("  [riglogic created]\n");
    auto* instance = rl4::RigInstance::create(rigLogic);
    const auto t_build = std::chrono::steady_clock::now();

    instance->setLOD(static_cast<std::uint16_t>(args.lod));

    const std::uint16_t rawCount = instance->getRawControlCount();
    const std::uint16_t guiCount = instance->getGUIControlCount();
    const std::uint16_t psdCount = instance->getPSDControlCount();

    std::printf("%s\n", args.dna);
    std::printf("  dna read            %6.1f ms  (all layers except Geometry)\n",
                std::chrono::duration<double, std::milli>(t_read - t_open).count());
    std::printf("  riglogic build      %6.1f ms\n",
                std::chrono::duration<double, std::milli>(t_build - t_read).count());
    std::printf("  LOD count           %6u   (using LOD %d)\n", rigLogic->getLODCount(), args.lod);
    std::printf("  gui controls        %6u\n", guiCount);
    std::printf("  raw controls        %6u\n", rawCount);
    std::printf("  psd controls        %6u\n", psdCount);
    std::printf("  ml controls         %6u\n", instance->getMLControlCount());
    std::printf("  rbf controls        %6u\n", instance->getRBFControlCount());

    // ---- one frame, from a known input ------------------------------------------------
    for (std::uint16_t i = 0; i < rawCount; ++i) {
        instance->setRawControl(i, controlValue(i, 0));
    }
    rigLogic->calculate(instance);

    const auto joints = instance->getJointOutputs();
    const auto shapes = instance->getBlendShapeOutputs();
    const auto maps = instance->getAnimatedMapOutputs();

    std::size_t nonZeroJoints = 0;
    float maxJoint = 0.0f;
    for (std::size_t i = 0; i < joints.size(); ++i) {
        if (joints[i] != 0.0f) { ++nonZeroJoints; }
        maxJoint = std::max(maxJoint, std::fabs(joints[i]));
    }

    std::printf("\n  joint outputs       %6zu  (%zu non-zero, max |v| %.4f)\n",
                joints.size(), nonZeroJoints, maxJoint);
    std::printf("  blendshape outputs  %6zu\n", shapes.size());
    std::printf("  animated maps       %6zu\n", maps.size());
    std::printf("  checksum joints     %.6f\n", checksum(joints));
    std::printf("  checksum shapes     %.6f\n", checksum(shapes));
    std::printf("  first 6 joint values ");
    for (std::size_t i = 0; i < 6 && i < joints.size(); ++i) {
        std::printf("%.5f ", joints[i]);
    }
    std::printf("\n");

    // A rig that evaluates to nothing would print a clean, plausible report above and mean
    // nothing at all. That is the failure this programme keeps re-learning, so it is an
    // explicit exit condition rather than something for a reader to notice.
    if (nonZeroJoints == 0) {
        std::fprintf(stderr, "\nFAIL: every joint output is zero — the rig did not evaluate.\n");
        return 3;
    }

    // ---- batch evaluation against a supplied control table -------------------------------
    //
    // This is the comparison that needs no frame of reference at all. The Python oracle and
    // this, Epic's own library, both emit in RigLogic's OWN output space — joint attribute
    // rows and blendshape weights, straight out of calculate(). No axis convention, no bone
    // hierarchy, no placement, none of the three things that produced three wrong tables when
    // comparing against Unreal's posed skeleton.
    //
    // Formats are deliberately dumb binary so neither side can disagree about parsing:
    //   controls: u32 poseCount, u32 controlCount, then poseCount*controlCount float32
    //   outputs:  u32 poseCount, u32 jointRows, u32 shapeCount,
    //             then per pose jointRows float32 followed by shapeCount float32
    if (args.controls != nullptr && args.outputs != nullptr) {
        std::FILE* cf = std::fopen(args.controls, "rb");
        if (cf == nullptr) {
            std::fprintf(stderr, "could not open %s\n", args.controls);
            return 1;
        }
        std::uint32_t poseCount = 0, controlCount = 0;
        if (std::fread(&poseCount, 4, 1, cf) != 1 || std::fread(&controlCount, 4, 1, cf) != 1) {
            std::fprintf(stderr, "short header in %s\n", args.controls);
            std::fclose(cf);
            return 1;
        }
        if (controlCount != rawCount) {
            std::fprintf(stderr,
                         "control table has %u controls, rig has %u raw controls. Refusing: a "
                         "silent truncation here would compare two different rigs.\n",
                         controlCount, rawCount);
            std::fclose(cf);
            return 1;
        }

        std::FILE* of = std::fopen(args.outputs, "wb");
        if (of == nullptr) {
            std::fprintf(stderr, "could not open %s for writing\n", args.outputs);
            std::fclose(cf);
            return 1;
        }
        const std::uint32_t jointRows = static_cast<std::uint32_t>(joints.size());
        const std::uint32_t shapeCount = static_cast<std::uint32_t>(shapes.size());
        std::fwrite(&poseCount, 4, 1, of);
        std::fwrite(&jointRows, 4, 1, of);
        std::fwrite(&shapeCount, 4, 1, of);

        std::vector<float> row(controlCount);
        for (std::uint32_t p = 0; p < poseCount; ++p) {
            if (std::fread(row.data(), 4, controlCount, cf) != controlCount) {
                std::fprintf(stderr, "control table ended at pose %u of %u\n", p, poseCount);
                std::fclose(cf);
                std::fclose(of);
                return 1;
            }
            for (std::uint16_t i = 0; i < rawCount; ++i) {
                instance->setRawControl(i, row[i]);
            }
            rigLogic->calculate(instance);
            const auto j = instance->getJointOutputs();
            const auto s = instance->getBlendShapeOutputs();
            std::fwrite(j.data(), 4, j.size(), of);
            std::fwrite(s.data(), 4, s.size(), of);
        }
        std::fclose(cf);
        std::fclose(of);
        std::printf("\n  evaluated %u poses -> %s (%u joint rows, %u shapes each)\n",
                    poseCount, args.outputs, jointRows, shapeCount);
    }

    // ---- steady-state cost -------------------------------------------------------------
    double perFrameMs = 0.0;
    if (args.frames > 0) {
        for (std::uint16_t i = 0; i < rawCount; ++i) { instance->setRawControl(i, controlValue(i, 1)); }
        rigLogic->calculate(instance);  // warm

        const auto t0 = std::chrono::steady_clock::now();
        for (int f = 0; f < args.frames; ++f) {
            for (std::uint16_t i = 0; i < rawCount; ++i) {
                instance->setRawControl(i, controlValue(i, f));
            }
            rigLogic->calculate(instance);
        }
        const auto t1 = std::chrono::steady_clock::now();
        perFrameMs = std::chrono::duration<double, std::milli>(t1 - t0).count() / args.frames;

        std::printf("\n  %d frames\n", args.frames);
        std::printf("  per frame           %8.4f ms\n", perFrameMs);
        std::printf("  sustained           %8.0f fps\n", 1000.0 / perFrameMs);
        std::printf("  of a 16.67 ms frame %8.2f %%\n", perFrameMs / (1000.0 / 60.0) * 100.0);
    }

    if (args.json != nullptr) {
        if (std::FILE* f = std::fopen(args.json, "w")) {
            std::fprintf(f,
                "{\n  \"dna\": \"%s\",\n  \"lod\": %d,\n  \"gui_controls\": %u,\n"
                "  \"raw_controls\": %u,\n  \"psd_controls\": %u,\n  \"joint_outputs\": %zu,\n"
                "  \"joint_outputs_nonzero\": %zu,\n  \"blend_shape_outputs\": %zu,\n"
                "  \"animated_maps\": %zu,\n  \"checksum_joints\": %.6f,\n"
                "  \"checksum_shapes\": %.6f,\n  \"frames\": %d,\n  \"per_frame_ms\": %.6f\n}\n",
                args.dna, args.lod, guiCount, rawCount, psdCount, joints.size(), nonZeroJoints,
                shapes.size(), maps.size(), checksum(joints), checksum(shapes),
                args.frames, perFrameMs);
            std::fclose(f);
            std::printf("  wrote %s\n", args.json);
        }
    }

    rl4::RigInstance::destroy(instance);
    rl4::RigLogic::destroy(rigLogic);
    dna::BinaryStreamReader::destroy(reader);
    trio::FileStream::destroy(stream);
    return 0;
}
