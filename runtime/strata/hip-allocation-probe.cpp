#include <hip/hip_runtime.h>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

void check(hipError_t e) {
    if (e != hipSuccess) { std::cerr << hipGetErrorString(e) << '\n'; std::exit(2); }
}
unsigned long long file_value(const std::filesystem::path& p) {
    unsigned long long value = 0; std::ifstream(p) >> value; return value;
}
void snapshot(const char* phase) {
    size_t free = 0, total = 0;
    check(hipMemGetInfo(&free, &total));
    unsigned long long available = 0, swapfree = 0, anon = 0, rss = 0, vram = 0, gtt = 0;
    std::string line, key, unit; unsigned long long value;
    std::ifstream mem("/proc/meminfo");
    while (std::getline(mem, line)) {
        std::istringstream s(line); s >> key >> value >> unit;
        if (key == "MemAvailable:") available = value * 1024;
        if (key == "SwapFree:") swapfree = value * 1024;
    }
    std::ifstream smap("/proc/self/smaps_rollup");
    while (std::getline(smap, line)) {
        std::istringstream s(line); s >> key >> value;
        if (key == "Anonymous:") anon = value * 1024;
        if (key == "Rss:") rss = value * 1024;
    }
    for (const auto& d : std::filesystem::directory_iterator("/sys/class/drm")) {
        const auto name = d.path().filename().string();
        if (name.rfind("card", 0) != 0 || name.find('-') != std::string::npos) continue;
        vram += file_value(d.path()/"device/mem_info_vram_used");
        gtt += file_value(d.path()/"device/mem_info_gtt_used");
    }
    std::cout << "{\"phase\":\"" << phase << "\",\"free\":" << free << ",\"total\":" << total
              << ",\"available\":" << available << ",\"swapfree\":" << swapfree
              << ",\"anon\":" << anon << ",\"rss\":" << rss
              << ",\"vram\":" << vram << ",\"gtt\":" << gtt << "}\n" << std::flush;
}
int main(int argc, char** argv) {
    if (argc != 3) return 1;
    const size_t count = std::stoull(argv[1]), bytes = std::stoull(argv[2]);
    if (!count || count > 256 || !bytes || bytes > 1024ULL*1024*1024 || count*bytes > 1024ULL*1024*1024) return 1;
    check(hipSetDevice(0)); check(hipFree(nullptr));
    hipDeviceProp_t prop{}; check(hipGetDeviceProperties(&prop, 0));
    if (std::string(prop.gcnArchName).rfind("gfx1151", 0) != 0) return 1;
    // Initialize the memset path before the baseline so lazy runtime work is not charged to allocations.
    void* warm = nullptr; check(hipMalloc(&warm, 4096)); check(hipMemset(warm, 0, 4096));
    check(hipDeviceSynchronize()); check(hipFree(warm));
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    snapshot("before");
    std::vector<void*> pointers;
    for (size_t i=0; i<count; ++i) {
        void* p = nullptr; check(hipMalloc(&p, bytes)); pointers.push_back(p);
        check(hipMemset(p, 0x35, bytes));
    }
    check(hipDeviceSynchronize());
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    snapshot("allocated");
    for (auto p : pointers) check(hipFree(p));
    check(hipDeviceSynchronize());
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    snapshot("freed");
}
