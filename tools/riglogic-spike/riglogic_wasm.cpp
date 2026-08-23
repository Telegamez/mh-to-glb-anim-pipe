// A C API over Epic's RigLogic, for the browser worker. Slice 5.2b.
//
// The evaluator stays Epic's, unmodified — this file only exposes it. Everything that made
// slice 5.1's numbers is reachable here: same library, same DNA, same calculate().
//
// ## Why a C API and not embind
//
// The hot path is one call per frame returning ~7,830 floats. embind would marshal that into a
// JS array every frame; a raw pointer into the wasm heap lets the worker wrap it in a
// Float32Array view once and read it in place, with no per-frame allocation. The whole point of
// 5.1's 0.29 ms measurement was that the arithmetic is cheap, and it would be a poor trade to
// spend more than that in the binding layer.
//
// ## Lifetime
//
// One RigLogic and one RigInstance per module instance. `rl_init` is idempotent-by-replacement:
// calling it again tears the previous pair down first, so a worker that reloads a character
// cannot leak or, worse, keep evaluating the old rig against the new one's control indices.

#include <dna/BinaryStreamReader.h>
#include <dna/Configuration.h>
#include <riglogic/RigLogic.h>
#include <trio/streams/MemoryStream.h>

#include <cstdint>
#include <cstring>

#include <emscripten/emscripten.h>

namespace {

trio::MemoryStream* g_stream = nullptr;
dna::BinaryStreamReader* g_reader = nullptr;
rl4::RigLogic* g_rigLogic = nullptr;
rl4::RigInstance* g_instance = nullptr;

void teardown() {
    if (g_instance) { rl4::RigInstance::destroy(g_instance); g_instance = nullptr; }
    if (g_rigLogic) { rl4::RigLogic::destroy(g_rigLogic); g_rigLogic = nullptr; }
    if (g_reader) { dna::BinaryStreamReader::destroy(g_reader); g_reader = nullptr; }
    if (g_stream) { trio::MemoryStream::destroy(g_stream); g_stream = nullptr; }
}

}  // namespace

extern "C" {

// Returns 0 on success, non-zero on failure. `data` is the .dna bytes, already in the heap.
EMSCRIPTEN_KEEPALIVE int rl_init(const unsigned char* data, int length) {
    teardown();

    g_stream = trio::MemoryStream::create();
    g_stream->write(reinterpret_cast<const char*>(data), static_cast<std::size_t>(length));
    g_stream->seek(0);

    // Everything except Geometry. Behavior alone is NOT enough: the joint matrix indexes
    // controls up to 825 while raw (263) plus PSD (545) reach only 808, and the extra 18 are
    // RBF and ML solver outputs. Loading only Behavior makes calculate() index past the end of
    // the control buffer and abort. Established in slice 5.0, the hard way.
    const auto layers = dna::DataLayer::RBFBehavior
                        | dna::DataLayer::MachineLearnedBehavior
                        | dna::DataLayer::JointBehaviorMetadata
                        | dna::DataLayer::TwistSwingBehavior;
    g_reader = dna::BinaryStreamReader::create(g_stream, layers);
    g_reader->read();
    if (!dna::Status::isOk()) { teardown(); return 1; }

    g_rigLogic = rl4::RigLogic::create(g_reader);
    if (!g_rigLogic) { teardown(); return 2; }
    g_instance = rl4::RigInstance::create(g_rigLogic);
    if (!g_instance) { teardown(); return 3; }
    return 0;
}

EMSCRIPTEN_KEEPALIVE int rl_raw_control_count() {
    return g_instance ? g_instance->getRawControlCount() : 0;
}

EMSCRIPTEN_KEEPALIVE int rl_joint_output_count() {
    return g_instance ? static_cast<int>(g_instance->getJointOutputs().size()) : 0;
}

EMSCRIPTEN_KEEPALIVE int rl_blend_shape_count() {
    return g_instance ? static_cast<int>(g_instance->getBlendShapeOutputs().size()) : 0;
}

EMSCRIPTEN_KEEPALIVE int rl_lod_count() {
    return g_rigLogic ? g_rigLogic->getLODCount() : 0;
}

EMSCRIPTEN_KEEPALIVE void rl_set_lod(int lod) {
    if (g_instance) { g_instance->setLOD(static_cast<std::uint16_t>(lod)); }
}

EMSCRIPTEN_KEEPALIVE void rl_set_raw_control(int index, float value) {
    if (g_instance) { g_instance->setRawControl(static_cast<std::uint16_t>(index), value); }
}

// Bulk set — one call for a whole control vector rather than 263 boundary crossings.
EMSCRIPTEN_KEEPALIVE void rl_set_raw_controls(const float* values, int count) {
    if (!g_instance) { return; }
    const int n = g_instance->getRawControlCount();
    const int m = count < n ? count : n;
    for (int i = 0; i < m; ++i) {
        g_instance->setRawControl(static_cast<std::uint16_t>(i), values[i]);
    }
}

EMSCRIPTEN_KEEPALIVE void rl_calculate() {
    if (g_rigLogic && g_instance) { g_rigLogic->calculate(g_instance); }
}

// Pointers into the wasm heap. The caller wraps these in a Float32Array view ONCE; they stay
// valid until rl_init is called again. Copying per frame is what this exists to avoid.
EMSCRIPTEN_KEEPALIVE const float* rl_joint_outputs_ptr() {
    return g_instance ? g_instance->getJointOutputs().data() : nullptr;
}

EMSCRIPTEN_KEEPALIVE const float* rl_blend_shape_outputs_ptr() {
    return g_instance ? g_instance->getBlendShapeOutputs().data() : nullptr;
}

EMSCRIPTEN_KEEPALIVE void rl_shutdown() { teardown(); }

}  // extern "C"
