0. 我们推荐将你的 GPU 驱动版本升级到最新（https://www.intel.com/content/www/us/en/download/785597/intel-arc-iris-xe-graphics-windows.html） 

1. 把zip文件解压到文件夹

2. 下载需要的 GGUF 模型，可以从 HuggingFace 或者 ModelScope 下载。

3. 如何使用 Llama.cpp:
   - 打开命令提示符（cmd），并通过在命令行输入指令 "cd /d PATH\TO\EXTRACTED\FOLDER" 进入解压缩后的文件夹
   - 设置推荐的环境变量:
     set SYCL_CACHE_PERSISTENT=1
   - 设置可选的环境变量:
     set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1 (启动此变量通常可以提高性能，但也有例外情况。因此，建议你在启用和禁用该环境变量的情况下进行测试，以找到最佳的性能设置。)
   - 在命令提示符（cmd）中，执行 "llama-cli.exe -m DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf -n 32 --prompt "What's AI?" -c 256 -t 8 -e -ngl 99" (你也可以使用其他的模型或者命令)

4. 可选设置以及多显卡使用方法:
   - 可选设置:
     set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1 (环境变量 SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS 用于控制是否使用即时命令列表将任务提交到 GPU。启动此变量通常可以提高性能，但有些机器也会出现性能下降的情况。因此，建议你在启用和禁用该环境变量的情况下进行测试，以找到最佳的性能设置。)
   - 多显卡使用方法: 默认配置会使用所有的显卡, llama-cli.exe 的日志会显示你当前拥有哪些显卡。根据你的配置，你可以使用下面的参数来选择使用一张或者多张显卡。
     set ONEAPI_DEVICE_SELECTOR=level_zero:0 (对于有多块显卡的用户,如果限制只使用一张显卡，可以使用本参数选择显卡，样例参数将使用第一张显卡) 
     set ONEAPI_DEVICE_SELECTOR="level_zero:0;level_zero:1" (对于有多块显卡的用户,如果要使用多张显卡，可以使用本参数选择多个显卡，样例参数将使用第一张和第二张显卡)
