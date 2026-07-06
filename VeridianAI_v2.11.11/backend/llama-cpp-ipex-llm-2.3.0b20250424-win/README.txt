0. We recommend updating your GPU driver to the latest (https://www.intel.com/content/www/us/en/download/785597/intel-arc-iris-xe-graphics-windows.html)

1. Extract the zip file to a folder

2. Prepare your gguf models, you can download from HuggingFace or ModelScope.

3. Run Llama.cpp as follows:
   - Open "Command Prompt" (cmd), enter the extracted folder by "cd /d PATH\TO\EXTRACTED\FOLDER"
   - Set recommended environment:
     set SYCL_CACHE_PERSISTENT=1
   - Run "llama-cli.exe -m DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf -n 32 --prompt "What's AI?" -c 256 -t 8 -e -ngl 99" in the "Command Prompt" (you may use any other model or command)

4. Optional setting and multi-GPUs usage:
   - Optional setting:
     set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1 (The environment variable SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS determines the usage of immediate command lists for task submission to the GPU. Under most circumstances, the following environment variable may improve performance, but sometimes this may also cause performance degradation.)
   - Multi-GPUs usage: the default configuration will use all your GPUs, the log of llama-cli.exe shows how many GPUs you have. According to your configuration, you can use below environment to determine which GPUs you want to use.
     set ONEAPI_DEVICE_SELECTOR=level_zero:0 (If you want to run on one GPU, llama.cpp will use the first GPU.) 
     set ONEAPI_DEVICE_SELECTOR="level_zero:0;level_zero:1" (If you want to run on two GPUs, llama.cpp will use the first and second GPUs.)
