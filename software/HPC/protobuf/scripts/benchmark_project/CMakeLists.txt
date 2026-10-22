cmake_minimum_required(VERSION 3.16)
project(protobuf_benchmark_adapter LANGUAGES CXX)

if(NOT IS_DIRECTORY "${BENCHMARK_SOURCE_DIR}")
  message(FATAL_ERROR "BENCHMARK_SOURCE_DIR must point to the pinned protobuf-benchmark directory")
endif()

find_package(Protobuf CONFIG REQUIRED)
find_package(benchmark CONFIG REQUIRED)

set(PROTO_NAMES
  scalar_int32 scalar_uint32 scalar_sint32 scalar_int64 scalar_uint64
  scalar_sint64 scalar_bool scalar_enum scalar_double scalar_float
  repeated_int32 repeated_uint32 repeated_sint32 repeated_int64
  repeated_uint64 repeated_sint64 repeated_double repeated_float
  string bytes_data
)

set(GENERATED_SOURCES)
foreach(NAME IN LISTS PROTO_NAMES)
  set(PROTO_SOURCE "${BENCHMARK_SOURCE_DIR}/${NAME}.proto")
  set(GENERATED_SOURCE "${CMAKE_CURRENT_BINARY_DIR}/${NAME}.pb.cc")
  set(GENERATED_HEADER "${CMAKE_CURRENT_BINARY_DIR}/${NAME}.pb.h")
  add_custom_command(
    OUTPUT "${GENERATED_SOURCE}" "${GENERATED_HEADER}"
    COMMAND protobuf::protoc
      "--proto_path=${BENCHMARK_SOURCE_DIR}"
      "--cpp_out=${CMAKE_CURRENT_BINARY_DIR}"
      "${PROTO_SOURCE}"
    DEPENDS "${PROTO_SOURCE}" protobuf::protoc
    VERBATIM
  )
  list(APPEND GENERATED_SOURCES "${GENERATED_SOURCE}")
endforeach()

add_library(protobuf_benchmark_proto STATIC ${GENERATED_SOURCES})
target_link_libraries(protobuf_benchmark_proto PUBLIC protobuf::libprotobuf)
target_include_directories(protobuf_benchmark_proto PUBLIC "${CMAKE_CURRENT_BINARY_DIR}")

add_executable(bm
  "${BENCHMARK_SOURCE_DIR}/benchmark_main.cpp"
  "${BENCHMARK_SOURCE_DIR}/benchmark_scalar.cpp"
  "${BENCHMARK_SOURCE_DIR}/benchmark_repeated.cpp"
  "${BENCHMARK_SOURCE_DIR}/benchmark_string.cpp"
)
target_compile_features(bm PRIVATE cxx_std_23)
target_include_directories(bm PRIVATE "${BENCHMARK_SOURCE_DIR}" "${CMAKE_CURRENT_BINARY_DIR}")
target_link_libraries(bm PRIVATE protobuf_benchmark_proto benchmark::benchmark)
add_dependencies(bm protobuf_benchmark_proto)

add_executable(protobuf_fixture_dump fixture_dump.cpp)
target_compile_features(protobuf_fixture_dump PRIVATE cxx_std_23)
target_include_directories(protobuf_fixture_dump PRIVATE "${BENCHMARK_SOURCE_DIR}")
target_link_libraries(protobuf_fixture_dump PRIVATE protobuf_benchmark_proto benchmark::benchmark)
add_dependencies(protobuf_fixture_dump protobuf_benchmark_proto)
