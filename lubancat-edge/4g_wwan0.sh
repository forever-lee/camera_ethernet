#!/bin/bash

# ============================================================
# Ubuntu / 鲁班猫 网络路由自动配置脚本
#
# 当前拓扑：
#   1. 4G 拨号上网接口：wwan0
#   2. eth1：连接车载交换机 / 摄像头网段
#   3. eth0：鲁班猫直连 Windows 电脑
#
# 目标：
#   1. 检查默认上网是否走 wwan0
#   2. 如果不是，则将 4G 接口 wwan0 调整为默认路由
#   3. 降低 eth1 的优先级，仅用于交换机 / 摄像头局域网
#   4. 保留 eth0 直连电脑网段访问能力
#   5. 不写死 eth0 / eth1 的局域网 IP，自动读取系统已有子网路由
#
# 注意：
#   - metric 数值越小，路由优先级越高
#   - 默认上网：wwan0
#   - 交换机 / 摄像头：eth1
#   - 直连 Windows：eth0
# ============================================================

set -u

# -----------------------------
# 基本参数配置
# -----------------------------

# 4G 拨号接口名称
G4_IF="wwan0"

# 连接交换机 / 摄像头的有线网口
SWITCH_IF="eth1"

# 直连 Windows 电脑的有线网口
PC_IF="eth0"

# 4G 默认路由优先级
G4_METRIC=50

# eth1 连接交换机，优先级低于 wwan0
SWITCH_METRIC=500

# eth0 直连电脑，保留直连访问能力
PC_METRIC=101

# 等待 4G 接口上线的最大时间，单位：秒
WAIT_TIME=60

# 用于测试公网路由的目标 IP
TEST_IP="8.8.8.8"


# -----------------------------
# 工具函数
# -----------------------------

log_info() {
    echo "[INFO] $*"
}

log_warn() {
    echo "[WARN] $*"
}

log_error() {
    echo "[ERROR] $*"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log_error "请使用 root 权限执行，例如：sudo $0"
        exit 1
    fi
}

wait_interface() {
    local ifname="$1"
    local count=0

    log_info "等待接口 $ifname 上线..."

    while ! ip link show "$ifname" >/dev/null 2>&1; do
        sleep 2
        count=$((count + 2))

        if [ "$count" -ge "$WAIT_TIME" ]; then
            log_error "等待 $ifname 超时，请检查接口名称或硬件连接"
            exit 1
        fi
    done

    ip link set "$ifname" up >/dev/null 2>&1 || true
    log_info "接口 $ifname 已存在"
}

wait_ipv4() {
    local ifname="$1"
    local count=0

    log_info "等待 $ifname 获取 IPv4 地址..."

    while ! ip -4 addr show "$ifname" | grep -q "inet "; do
        sleep 2
        count=$((count + 2))

        if [ "$count" -ge "$WAIT_TIME" ]; then
            log_error "$ifname 未获取 IPv4 地址"
            exit 1
        fi
    done

    local ip_addr
    ip_addr=$(ip -4 addr show "$ifname" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n 1)
    log_info "$ifname IPv4 地址为：$ip_addr"
}

set_nm_connection_metric() {
    local ifname="$1"
    local metric="$2"
    local never_default="$3"

    if ! command -v nmcli >/dev/null 2>&1; then
        log_warn "系统未找到 nmcli，跳过 NetworkManager 配置：$ifname"
        return 0
    fi

    local conn
    conn=$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v dev="$ifname" '$2==dev {print $1; exit}')

    if [ -n "$conn" ]; then
        log_info "找到 $ifname 对应 NetworkManager 连接：$conn"
        nmcli connection modify "$conn" ipv4.route-metric "$metric" || true
        nmcli connection modify "$conn" ipv4.never-default "$never_default" || true
        log_info "已设置 $ifname：route-metric=$metric, never-default=$never_default"
    else
        log_warn "未找到 $ifname 对应的 NetworkManager 活跃连接，跳过 nmcli 配置"
    fi
}

delete_default_routes_by_dev() {
    local ifname="$1"

    while ip route show default | grep -q " dev $ifname"; do
        local route_line
        route_line=$(ip route show default | grep " dev $ifname" | head -n 1)
        log_info "删除默认路由：$route_line"
        ip route del $route_line || break
    done
}

delete_default_routes_not_4g() {
    log_info "清理非 $G4_IF 的默认路由..."

    while ip route show default | grep -v " dev $G4_IF" | grep -q "^default"; do
        local route_line
        route_line=$(ip route show default | grep -v " dev $G4_IF" | grep "^default" | head -n 1)
        log_info "删除非 4G 默认路由：$route_line"
        ip route del $route_line || break
    done
}

get_wwan_gateway() {
    # 优先从已有 wwan0 默认路由中提取 via 网关
    local gw
    gw=$(ip route show default dev "$G4_IF" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="via") print $(i+1)}' | head -n 1)

    if [ -n "$gw" ]; then
        echo "$gw"
        return 0
    fi

    # 其次从任意 wwan0 路由中提取 via
    gw=$(ip route show dev "$G4_IF" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="via") print $(i+1)}' | head -n 1)

    if [ -n "$gw" ]; then
        echo "$gw"
        return 0
    fi

    return 1
}

set_4g_default_route() {
    log_info "检查公网访问 $TEST_IP 当前走哪个接口..."

    local current_dev
    current_dev=$(ip route get "$TEST_IP" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')

    if [ "$current_dev" = "$G4_IF" ]; then
        log_info "当前公网访问已经走 $G4_IF"
    else
        log_warn "当前公网访问不是走 $G4_IF，而是：${current_dev:-未知}，准备调整默认路由"
    fi

    local gw
    gw=""
    if gw=$(get_wwan_gateway); then
        log_info "检测到 $G4_IF 网关：$gw"
    else
        log_warn "未检测到 $G4_IF 网关，将尝试使用 default dev $G4_IF"
    fi

    # 删除全部 wwan0 旧默认路由，避免 metric 700 等旧路由残留
    delete_default_routes_by_dev "$G4_IF"

    if [ -n "$gw" ]; then
        ip route replace default via "$gw" dev "$G4_IF" metric "$G4_METRIC"
    else
        ip route replace default dev "$G4_IF" metric "$G4_METRIC"
    fi

    log_info "已设置默认路由走 $G4_IF，metric=$G4_METRIC"
}

adjust_link_route_metric() {
    local ifname="$1"
    local metric="$2"

    log_info "检查 $ifname 的局域网子网路由..."

    local routes
    routes=$(ip route show dev "$ifname" | grep "proto kernel" | grep "scope link" || true)

    if [ -z "$routes" ]; then
        log_warn "未找到 $ifname 的 proto kernel scope link 子网路由，可能该接口未配置 IPv4"
        return 0
    fi

    while IFS= read -r route_line; do
        [ -z "$route_line" ] && continue

        local subnet
        local src
        subnet=$(echo "$route_line" | awk '{print $1}')
        src=$(echo "$route_line" | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')

        if [ -n "$src" ]; then
            ip route replace "$subnet" dev "$ifname" proto kernel scope link src "$src" metric "$metric"
        else
            ip route replace "$subnet" dev "$ifname" proto kernel scope link metric "$metric"
        fi

        log_info "已设置 $ifname 局域网路由：$subnet metric=$metric"
    done <<< "$routes"
}


# -----------------------------
# 主流程
# -----------------------------

require_root

log_info "开始配置网络路由策略"
log_info "4G 上网接口：$G4_IF"
log_info "交换机接口：$SWITCH_IF"
log_info "直连电脑接口：$PC_IF"

# Step 1：等待 wwan0 和 IPv4 地址
wait_interface "$G4_IF"
wait_ipv4 "$G4_IF"

# Step 2：NetworkManager 层面设置路由优先级
# wwan0 允许作为默认路由；eth1 / eth0 不允许作为默认路由
set_nm_connection_metric "$G4_IF" "$G4_METRIC" "no"
set_nm_connection_metric "$SWITCH_IF" "$SWITCH_METRIC" "yes"
set_nm_connection_metric "$PC_IF" "$PC_METRIC" "yes"

# Step 3：清理 eth1 / eth0 默认路由，避免有线网口抢默认上网
delete_default_routes_by_dev "$SWITCH_IF"
delete_default_routes_by_dev "$PC_IF"

# Step 4：清理其他非 wwan0 默认路由
delete_default_routes_not_4g

# Step 5：设置 wwan0 为默认路由
set_4g_default_route

# Step 6：保留并调整 eth1 / eth0 的局域网直连路由
adjust_link_route_metric "$SWITCH_IF" "$SWITCH_METRIC"
adjust_link_route_metric "$PC_IF" "$PC_METRIC"

# Step 7：输出最终结果
log_info "当前 IPv4 路由表："
ip route

log_info "公网访问路由测试：$TEST_IP"
ip route get "$TEST_IP" || true

log_info "eth1 交换机网段路由："
ip route show dev "$SWITCH_IF" || true

log_info "eth0 直连电脑网段路由："
ip route show dev "$PC_IF" || true

log_info "路由配置完成"
