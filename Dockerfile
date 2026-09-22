# game-harness 容器：一个容器里跑三个服务
#   网页后端 8780（对外） + NGA 爬虫 8770 + bilibili 爬虫 8771（同容器内走回环）
#
# 为什么是一个容器、不是三个：
#   两个爬虫服务把 127.0.0.1 写死在代码里（nga_search_server.py / bili_search_server.py 的
#   _QuietServer 那行），而 harness/supervisor.py 的「按需拉起、闲置关停」也要求三个进程
#   同机同环境。拆成独立容器就得改爬虫的绑定地址，还会把按需拉起这套机制整个废掉。
#
# 爬虫从哪来：
#   两个爬虫是**独立仓库**（不在主仓里），构建时 git clone 下来。这样主仓只装问答本体，
#   爬虫能各自更新，用户也不用手动放目录。想换地址/换分支就用 --build-arg 覆盖下面三个 ARG。
#
# 钉死 bookworm（Debian 12）：`python:3.11-slim` 现在指向 Debian 13，
# 而 playwright install --with-deps 只认到 12，用 13 会报「不支持的发行版」。
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

ARG CRAWLER_NGA_REPO=https://github.com/MaIOqwq/game-harness-crawler-nga.git
ARG CRAWLER_BILI_REPO=https://github.com/MaIOqwq/game-harness-crawler-bili.git
ARG CRAWLER_REF=main

# 依赖清单先拷进来 —— 改代码不会让下面这层缓存失效
COPY requirements.txt /tmp/req/harness.txt

# Debian 默认源 deb.debian.org 走 80 口，国内直连不通；换成阿里镜像。
# （443 口是通的，但没必要绕，换成镜像反而快。）git 是给下面 clone 爬虫用的。
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        if [ -f "$f" ]; then sed -i 's|deb.debian.org|mirrors.aliyun.com|g' "$f"; fi; \
    done; \
    apt-get -o Acquire::Retries=5 update; \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        ca-certificates git fonts-noto-cjk libgl1 libglib2.0-0

# 必须给个工作目录再干活：直接往根目录塞的话，包里那个 bin/ 会撞上容器的 /bin
# （那是指向 /usr/bin 的符号链接），构建报 cannot copy to non-directory。
WORKDIR /app

# 国内直连 github.com 常被断(实测: 容器里 443 直接 Connection refused)。
# 先直连、失败了再走这个镜像前缀重试 —— 镜像只在下不来时才用得上, 直连能通的地方它不参与。
# 想换镜像或走自己的代理, --build-arg GITHUB_MIRROR=... 覆盖。
# 置空 = 只用直连。那样也能构建(直连能通的话), 直连不通时这一层会直接红、报错看得懂。
ARG GITHUB_MIRROR=https://ghfast.top/

# 取两个爬虫。改 CRAWLER_REF（或加 --no-cache）才会重新拉，否则走缓存。
# 每个仓库先直连、失败再走镜像前缀 —— 两条都试，是为了不管在哪儿构建都能成。
RUN set -eux; \
    export GIT_TERMINAL_PROMPT=0; \
    mkdir -p crawlers; \
    clone() { \
        git clone --depth 1 --branch "$CRAWLER_REF" "$1" "$2" && return 0; \
        if [ -n "$GITHUB_MIRROR" ]; then \
            echo "直连 $1 不通, 改走镜像 ${GITHUB_MIRROR}$1"; \
            git clone --depth 1 --branch "$CRAWLER_REF" "$GITHUB_MIRROR$1" "$2" && return 0; \
        fi; \
        echo "两个地址都取不下来: $1"; \
        return 1; \
    }; \
    clone "$CRAWLER_NGA_REPO" crawlers/nga; \
    clone "$CRAWLER_BILI_REPO" crawlers/bili; \
    rm -rf crawlers/nga/.git crawlers/bili/.git

# 三步分开：网慢，前面装好的不该因为后面失败而作废
RUN pip install -r /tmp/req/harness.txt -r crawlers/nga/requirements.txt -r crawlers/bili/requirements.txt

# --with-deps 会把浏览器要的系统库补齐（它自己会再跑一次 apt-get update）
RUN python -m playwright install --with-deps chromium chrome

RUN rm -rf /var/lib/apt/lists/* /tmp/req

# crawlers/ 在 .dockerignore 里被排掉（本机那份可能带登录态），上面 clone 的那份不会被覆盖
COPY . .

# 两个爬虫在 /app/crawlers 下、由 supervisor 按需拉起
ENV HARNESS_LOCAL=1 \
    HARNESS_LOCAL_ROOT=/app/crawlers

EXPOSE 8780

# 必须 --host 0.0.0.0：默认是 127.0.0.1，那样容器外面映射不进来
CMD ["python", "-u", "-m", "harness.webapp", "--port", "8780", "--host", "0.0.0.0"]
