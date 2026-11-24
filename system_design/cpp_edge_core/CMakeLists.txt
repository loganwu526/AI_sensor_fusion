cmake_minimum_required(VERSION 3.16)

project(cpp_edge_core
    VERSION 1.0.0
    DESCRIPTION "四模態 AI 感測融合嵌入式 C++20 邊緣運算核心"
    LANGUAGES CXX
)

# 強制使用 C++20 標準且關閉編譯器專屬擴充
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 預設 Release / RelWithDebInfo 若未指定則採用 -O2 最佳化檢驗防 DSE 行為
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "建置組態" FORCE)
endif()

# 靜態核心融合函式庫（Zero Heap Allocation 熱路徑）
add_library(edge_fusion_core STATIC
    src/fusion_core.cpp
)

target_include_directories(edge_fusion_core
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
)

# 嚴格編譯警告旗標（確保零警告編譯通過）
if(MSVC)
    target_compile_options(edge_fusion_core PUBLIC /W4 /utf-8)
else()
    target_compile_options(edge_fusion_core PUBLIC
        -Wall
        -Wextra
        -Wpedantic
        -Wconversion
        -Wshadow
    )
endif()

# 示範與微基準測試執行檔
add_executable(edge_core_demo
    src/main.cpp
)
target_link_libraries(edge_core_demo PRIVATE edge_fusion_core)

# C++20 單元測試執行檔
add_executable(test_edge_core
    tests/test_edge_core.cpp
)
target_link_libraries(test_edge_core PRIVATE edge_fusion_core)

# MinGW 環境採靜態連結，避免雙擊執行時因缺少 libstdc++-6 / libwinpthread DLL 閃退
if(MINGW)
    target_link_options(edge_core_demo PRIVATE -static)
    target_link_options(test_edge_core PRIVATE -static)
endif()

# 註冊 CTest
enable_testing()
add_test(NAME EdgeCoreUnitTests COMMAND test_edge_core)
