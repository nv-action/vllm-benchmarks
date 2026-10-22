#include "benchmark_common.h"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

template <typename Message>
void write_fixture(const std::filesystem::path &directory, const std::string &name,
                   std::size_t count, const Message &message) {
    std::string bytes;
    if (!message.SerializeToString(&bytes)) {
        throw std::runtime_error("fixture serialization failed: " + name);
    }
    const auto path = directory / (name + "-" + std::to_string(count) + ".pb");
    std::ofstream output(path, std::ios::binary);
    output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    if (!output) {
        throw std::runtime_error("fixture write failed: " + path.string());
    }
}

int main(int argc, char **argv) {
    if (argc != 2) {
        std::cerr << "usage: protobuf_fixture_dump OUTPUT_DIR\n";
        return 2;
    }
    try {
        const std::filesystem::path directory(argv[1]);
        std::filesystem::create_directories(directory);
        for (std::size_t count : {10U, 100U, 1000U, 10000U}) {
#define WRITE_FIXTURE(name) write_fixture(directory, #name, count, gen##name(count))
            WRITE_FIXTURE(ScalarInt32);
            WRITE_FIXTURE(ScalarUInt32);
            WRITE_FIXTURE(ScalarSInt32);
            WRITE_FIXTURE(ScalarInt64);
            WRITE_FIXTURE(ScalarUInt64);
            WRITE_FIXTURE(ScalarSInt64);
            WRITE_FIXTURE(ScalarBool);
            WRITE_FIXTURE(ScalarEnum);
            WRITE_FIXTURE(ScalarDouble);
            WRITE_FIXTURE(ScalarFloat);
            WRITE_FIXTURE(RepeatedInt32);
            WRITE_FIXTURE(RepeatedUInt32);
            WRITE_FIXTURE(RepeatedSInt32);
            WRITE_FIXTURE(RepeatedInt64);
            WRITE_FIXTURE(RepeatedUInt64);
            WRITE_FIXTURE(RepeatedSInt64);
            WRITE_FIXTURE(RepeatedDouble);
            WRITE_FIXTURE(RepeatedFloat);
#undef WRITE_FIXTURE
            write_fixture(directory, "StringASCII", count,
                          makeStringData(count, Scene::kPureAscii));
            write_fixture(directory, "StringChinese", count,
                          makeStringData(count, Scene::kChinese));
            write_fixture(directory, "Bytes", count, makeBytesData(count));
        }
    } catch (const std::exception &error) {
        std::cerr << "fixture generation failed: " << error.what() << '\n';
        return 1;
    }
    return 0;
}
