#include "linux_input.h"
#include "lvgl.h"

#if USE_DESKTOP
#include "src/drivers/sdl/lv_sdl_window.h"
#include <SDL.h>
#else
#include "src/drivers/display/fb/lv_linux_fbdev.h"
#endif

#include <algorithm>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <fstream>
#include <string>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

namespace {
constexpr const char* broadcast = "4294967295";
constexpr const char* tabs[] = {"Inbox", "Nodes", "GPS", "Settings"};
constexpr const char* regions[] = {"US", "EU_868", "ANZ", "NZ_865", "KR", "TW", "JP", "SG_923", "TH", "MY_919", "PH_915"};
constexpr const char* presets[] = {"LONG_FAST", "LONG_MODERATE", "LONG_SLOW", "MEDIUM_FAST", "MEDIUM_SLOW", "SHORT_FAST", "SHORT_SLOW"};
volatile std::sig_atomic_t stopping = 0;

using Fields = std::vector<std::string>;

Fields split(const std::string& line) {
    Fields result;
    size_t begin = 0;
    for (size_t end = line.find('\t'); end != std::string::npos; end = line.find('\t', begin)) {
        result.push_back(line.substr(begin, end - begin));
        begin = end + 1;
    }
    result.push_back(line.substr(begin));
    return result;
}

struct Item {
    std::string title;
    std::string subtitle;
};

struct MeshScreen {
    int wire;
    int tab = 0;
    int cursor = 0;
    lv_obj_t* root = nullptr;
    lv_obj_t* editor = nullptr;
    std::string input;
    std::string choice;
    std::vector<std::string> options;
    std::string status = "Starting";
    std::string notice;
    lv_obj_t* edit_notice = nullptr;
    std::string region = "UNSET";
    std::string preset = "LONG_FAST";
    std::string owner;
    std::string own_id;
    std::string gps = "Waiting for radio";
    std::string action;
    std::string target;
    std::string destination = broadcast;
    std::string channel = "0";
    std::vector<Fields> messages, nodes, channels;
    std::vector<Item> rows;
    bool pending_send = false;
    bool choosing_channel = false;
    bool reading_message = false;

    void request(const std::string& line) {
        const std::string frame = line + "\n";
        if (send(wire, frame.data(), frame.size(), 0) != static_cast<ssize_t>(frame.size())) {
            notice = "Radio connection lost";
            pending_send = false;
            render();
        }
    }

    void field(const Fields& f) {
        if (f[0] == "reset") {
            messages.clear();
            nodes.clear();
            channels.clear();
        } else if (f[0] == "status" && f.size() == 2) {
            status = f[1];
        } else if (f[0] == "notice" && f.size() == 2) {
            notice = f[1];
            pending_send = false;
            if (action.empty() && !reading_message) render();
            else if (edit_notice) lv_label_set_text(edit_notice, notice.c_str());
        } else if (f[0] == "sent" && f.size() == 2) {
            notice = f[1];
            pending_send = false;
            action.clear();
            editor = nullptr;
            render();
        } else if (f[0] == "settings" && f.size() == 4) {
            owner = f[1];
            region = f[2];
            preset = f[3];
        } else if (f[0] == "identity" && f.size() == 2) {
            own_id = f[1];
        } else if (f[0] == "gps" && f.size() == 2) {
            gps = f[1];
        } else if (f[0] == "node" && f.size() == 5) {
            nodes.push_back(f);
        } else if (f[0] == "channel" && f.size() == 3) {
            channels.push_back(f);
        } else if (f[0] == "message" && f.size() == 6) {
            messages.push_back(f);
        } else if (f[0] == "ready" && action.empty() && !reading_message) {
            render();
        }
    }

    std::string node_name(const std::string& id) const {
        if (id == own_id) return "You";
        for (const auto& node : nodes) if (node[1] == id) return node[2];
        return "Node " + id;
    }

    std::string channel_name(const std::string& index) const {
        for (const auto& ch : channels) if (ch[1] == index) return ch[2];
        return "Channel " + index;
    }

    lv_obj_t* label(lv_obj_t* parent, const std::string& text, int x, int y, int width,
                    unsigned color, const lv_font_t* font = &lv_font_montserrat_12,
                    lv_label_long_mode_t mode = LV_LABEL_LONG_DOT) {
        auto* obj = lv_label_create(parent);
        lv_obj_set_pos(obj, x, y);
        lv_obj_set_width(obj, width);
        lv_obj_set_style_text_font(obj, font, 0);
        lv_obj_set_style_text_color(obj, lv_color_hex(color), 0);
        lv_label_set_long_mode(obj, mode);
        lv_label_set_text(obj, text.c_str());
        return obj;
    }

    lv_obj_t* panel(int x, int y, int w, int h, unsigned color) {
        auto* obj = lv_obj_create(root);
        lv_obj_remove_style_all(obj);
        lv_obj_set_pos(obj, x, y);
        lv_obj_set_size(obj, w, h);
        lv_obj_set_style_radius(obj, 5, 0);
        lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(obj, lv_color_hex(color), 0);
        lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
        return obj;
    }

    void make_rows() {
        rows.clear();
        if (!choice.empty()) {
            for (const auto& value : options) rows.push_back({value, choice == "region" ? "Confirm your local radio region" : "Select modem preset"});
        } else if (choosing_channel) {
            for (const auto& ch : channels) rows.push_back({ch[2], "Channel " + ch[1]});
        } else if (tab == 0) {
            for (const auto& msg : messages) {
                const std::string kind = msg[2] == broadcast ? "#" + channel_name(msg[3]) : "Direct";
                const auto who = msg[1] == own_id && msg[2] != broadcast ? "To " + node_name(msg[2]) : node_name(msg[1]);
                rows.push_back({kind + "  /  " + who + "  /  " + msg[4], msg[5]});
            }
        } else if (tab == 1) {
            for (const auto& node : nodes) rows.push_back({node[2], node[3] + "  /  " + node[4]});
        } else if (tab == 2) {
            rows.push_back({"Local position", gps});
            rows.push_back({"Radio", status});
        } else {
            rows = {{"Region", region == "UNSET" ? "Choose a region to enable sending" : region},
                    {"Modem preset", preset}, {"Node name", owner.empty() ? "Set a name" : owner},
                    {"Import channel", "Add channels from a Meshtastic share URL"},
                    {"Compose channel", channel_name(channel)}};
        }
        cursor = std::clamp(cursor, 0, std::max(0, static_cast<int>(rows.size()) - 1));
    }

    void render() {
        lv_obj_clean(root);
        edit_notice = nullptr;
        lv_obj_set_style_bg_color(root, lv_color_hex(0x0b1621), 0);
        lv_obj_set_style_bg_opa(root, LV_OPA_COVER, 0);
        lv_obj_remove_flag(root, LV_OBJ_FLAG_SCROLLABLE);
        label(root, "MESH / ZERO", 10, 5, 132, 0xeff8ff, &lv_font_montserrat_14);
        label(root, status, 144, 7, 166, 0x6cddbc);
        for (int i = 0; i < 4; ++i) {
            auto* chip = panel(8 + i * 77, 25, 73, 21, i == tab ? 0x196758 : 0x172938);
            label(chip, tabs[i], 7, 3, 63, i == tab ? 0xffffff : 0xa8bbc9);
        }
        if (!action.empty()) {
            label(root, target, 10, 51, 298, 0x8ce8c9, &lv_font_montserrat_14);
            editor = lv_textarea_create(root);
            lv_obj_set_pos(editor, 10, 72);
            lv_obj_set_size(editor, 300, 64);
            lv_obj_set_style_text_font(editor, &lv_font_montserrat_14, 0);
            lv_obj_set_style_text_color(editor, lv_color_hex(0xffffff), 0);
            lv_obj_set_style_bg_color(editor, lv_color_hex(0x192d3d), 0);
            lv_obj_set_style_border_color(editor, lv_color_hex(0x367a72), 0);
            lv_textarea_set_max_length(editor, action == "channel-url" ? 700 : 200);
            lv_textarea_set_text(editor, "");
            edit_notice = label(root, "Enter: save or send  /  Esc: cancel", 10, 151, 300, 0xa8bbc9);
            return;
        }
        make_rows();
        if (rows.empty()) {
            auto* box = panel(8, 55, 304, 86, 0x172938);
            label(box, choosing_channel ? "No active channels" : tab == 0 ? "No messages yet" : "No nodes heard",
                  9, 9, 288, 0xf3f7ff, &lv_font_montserrat_14);
            label(box, status, 9, 33, 286, 0xa8bbc9, &lv_font_montserrat_12, LV_LABEL_LONG_WRAP);
        }
        int first = cursor / 3 * 3;
        for (int i = first; i < std::min(first + 3, static_cast<int>(rows.size())); ++i) {
            auto* box = panel(8, 52 + (i - first) * 31, 304, 29, i == cursor ? 0x244255 : 0x152635);
            if (i == cursor) {
                lv_obj_set_style_border_color(box, lv_color_hex(0x64dfbc), 0);
                lv_obj_set_style_border_side(box, LV_BORDER_SIDE_LEFT, 0);
                lv_obj_set_style_border_width(box, 3, 0);
            }
            label(box, rows[i].title, 8, 1, 287, i == cursor ? 0x87efcc : 0xc6e0ee);
            label(box, rows[i].subtitle, 8, 14, 287, 0xf3f7ff);
        }
        const std::string hint = !choice.empty() ? "Up/down: choose  /  Enter: confirm" : choosing_channel ? "Enter: select channel  /  Esc: back" :
            tab == 0 ? "N: compose  /  Enter: read  /  Esc: exit" :
            tab == 1 ? "Enter: direct message  /  Esc: exit" :
            tab == 3 ? "Enter: edit  /  Left and right: tabs" : "Left and right: tabs  /  R: refresh";
        label(root, notice.empty() ? hint : notice, 10, 151, 300, notice.empty() ? 0xa8bbc9 : 0xffdc99);
    }

    void read_message() {
        if (messages.empty()) return;
        const auto msg = messages[cursor];
        reading_message = true;
        lv_obj_clean(root);
        label(root, node_name(msg[1]) + "  /  " + msg[4], 10, 8, 300, 0x87efcc, &lv_font_montserrat_14);
        auto* box = panel(8, 32, 304, 110, 0x192d3d);
        lv_obj_add_flag(box, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scroll_dir(box, LV_DIR_VER);
        editor = box;
        label(box, msg[5], 8, 7, 288, 0xffffff, &lv_font_montserrat_14, LV_LABEL_LONG_WRAP);
        label(root, "Up and down: scroll  /  Esc: back", 10, 151, 300, 0xa8bbc9);
    }

    void compose(const std::string& node, const std::string& title) {
        if (region == "UNSET" || channels.empty()) {
            notice = channels.empty() ? "Radio offline or no channel enabled" : "Choose a region in Settings";
            render();
            return;
        }
        destination = node;
        action = "send";
        target = title;
        render();
    }

    template<size_t N>
    void choose(const char* op, const char* const (&values)[N], const std::string& current) {
        choice = op;
        options.assign(std::begin(values), std::end(values));
        const auto it = std::find(options.begin(), options.end(), current);
        cursor = it == options.end() ? 0 : static_cast<int>(it - options.begin());
        render();
    }

    void key(uint32_t value) {
        if (reading_message) {
            if (value == LV_KEY_ESC) {
                editor = nullptr;
                reading_message = false;
                render();
            } else if (value == LV_KEY_UP || value == LV_KEY_DOWN) {
                lv_obj_scroll_by(editor, 0, value == LV_KEY_UP ? 32 : -32, LV_ANIM_OFF);
            }
            return;
        }
        if (editor) {
            if (value == LV_KEY_ESC && !pending_send) {
                action.clear();
                editor = nullptr;
                render();
            } else if (value == LV_KEY_ENTER && !pending_send) {
                const std::string body = lv_textarea_get_text(editor);
                if (body.empty()) return;
                if (action == "send") {
                    pending_send = true;
                    request("send\t" + destination + "\t" + channel + "\t" + body);
                } else {
                    request(action + "\t" + body);
                    action.clear();
                    editor = nullptr;
                    render();
                }
            } else if (!pending_send && value == LV_KEY_BACKSPACE) lv_textarea_delete_char(editor);
            else if (!pending_send && value >= 32 && value < 127) lv_textarea_add_char(editor, value);
            return;
        }

        notice.clear();
        if (!choice.empty()) {
            if (value == LV_KEY_ENTER || value == LV_KEY_ESC) {
                if (value == LV_KEY_ENTER) request(choice + "\t" + options[cursor]);
                cursor = choice == "region" ? 0 : 1;
                choice.clear();
                render();
            } else if (value == LV_KEY_UP || value == 'f' || value == LV_KEY_DOWN || value == 'x') {
                cursor += value == LV_KEY_UP || value == 'f' ? -1 : 1;
                render();
            }
            return;
        }
        if (value == LV_KEY_ESC) {
            if (choosing_channel) { choosing_channel = false; cursor = 0; render(); }
            else stopping = 1;
        } else if (value == LV_KEY_ENTER && choosing_channel) {
            if (!channels.empty() && static_cast<size_t>(cursor) < channels.size()) channel = channels[cursor][1];
            choosing_channel = false;
            cursor = 0;
            render();
        } else if (value == LV_KEY_UP || value == LV_KEY_DOWN || value == 'f' || value == 'x') {
            cursor += value == LV_KEY_UP || value == 'f' ? -1 : 1;
            render();
        } else if (!choosing_channel && (value == LV_KEY_LEFT || value == LV_KEY_RIGHT || value == 'z' || value == 'c')) {
            tab = (tab + (value == LV_KEY_LEFT || value == 'z' ? 3 : 1)) % 4;
            cursor = 0;
            render();
        } else if (value == 'r') request("refresh");
        else if (tab == 0 && value == 'n') compose(broadcast, "#" + channel_name(channel));
        else if (tab == 0 && value == LV_KEY_ENTER) read_message();
        else if (tab == 1 && value == LV_KEY_ENTER && !nodes.empty()) compose(nodes[cursor][1], "To " + nodes[cursor][2]);
        else if (tab == 3 && value == LV_KEY_ENTER) {
            if (cursor == 0) choose("region", regions, region);
            else if (cursor == 1) choose("preset", presets, preset);
            else if (cursor == 2 || cursor == 3) {
                action = cursor == 2 ? "owner" : "channel-url";
                target = cursor == 2 ? "New node name" : "Channel share URL";
                render();
            }
            if (cursor == 4) { choosing_channel = true; cursor = 0; render(); }
        }
    }

    void poll() {
        char data[8192];
        for (;;) {
            const auto size = read(wire, data, sizeof(data));
            if (size < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) break;
            if (size <= 0) { stopping = 1; break; }
            input.append(data, static_cast<size_t>(size));
            size_t newline;
            while ((newline = input.find('\n')) != std::string::npos) {
                field(split(input.substr(0, newline)));
                input.erase(0, newline + 1);
            }
        }
    }
};

#if USE_DESKTOP
int sdl_key(void* data, SDL_Event* event) {
    if (event->type != SDL_KEYDOWN) return 0;
    const auto key = event->key.keysym.sym;
    uint32_t value = static_cast<uint32_t>(key);
    switch (key) {
        case SDLK_BACKSPACE: value = LV_KEY_BACKSPACE; break;
        case SDLK_RETURN: value = LV_KEY_ENTER; break;
        case SDLK_UP: value = LV_KEY_UP; break;
        case SDLK_DOWN: value = LV_KEY_DOWN; break;
        case SDLK_LEFT: value = LV_KEY_LEFT; break;
        case SDLK_RIGHT: value = LV_KEY_RIGHT; break;
        default:
            if (key >= 'a' && key <= 'z' && (event->key.keysym.mod & KMOD_SHIFT)) value -= 32;
            break;
    }
    static_cast<MeshScreen*>(data)->key(value);
    return 0;
}

void screenshot(lv_obj_t* root, const char* path) {
    auto* pixels = lv_snapshot_take(root, LV_COLOR_FORMAT_XRGB8888);
    if (!pixels) { std::fprintf(stderr, "LVGL snapshot failed\n"); return; }
    std::ofstream out(path, std::ios::binary);
    out << "P6\n320 170\n255\n";
    for (int y = 0; y < 170; ++y) {
        auto* row = pixels->data + y * pixels->header.stride;
        for (int x = 0; x < 320; ++x) {
            out.put(row[4 * x + 2]); out.put(row[4 * x + 1]); out.put(row[4 * x]);
        }
    }
    lv_draw_buf_destroy(pixels);
}
#endif
} // namespace

int main() {
    const char* descriptor = std::getenv("CZ_MESH_FD");
    if (!descriptor) return 1;
    const int wire = std::atoi(descriptor);
    std::signal(SIGTERM, [](int) { stopping = 1; });
    std::signal(SIGINT, [](int) { stopping = 1; });
    std::signal(SIGPIPE, SIG_IGN);
    if (fcntl(wire, F_SETFL, O_NONBLOCK) < 0) return 1;
    lv_init();
#if USE_DESKTOP
    auto* display = lv_sdl_window_create(320, 170);
    lv_sdl_window_set_title(display, "Mesh for CardputerZero");
    lv_sdl_window_set_zoom(display, 2);
#else
    auto* display = lv_linux_fbdev_create();
    if (!display || lv_linux_fbdev_set_file(display, "/dev/fb0") != LV_RESULT_OK) return 1;
    platform::init_key_input(display);
#endif
    if (!display) return 1;
    MeshScreen app{wire};
    app.root = lv_screen_active();
    app.render();
    platform::set_global_key_listener([](uint32_t key, const char*, bool held, void* data) {
        if (!held) static_cast<MeshScreen*>(data)->key(key);
        return true;
    }, &app);
#if USE_DESKTOP
    SDL_AddEventWatch(sdl_key, &app);
    const char* capture = std::getenv("CZ_MESH_SCREENSHOT");
    const uint32_t started = lv_tick_get();
#endif
    while (!stopping) {
        app.poll();
        lv_timer_handler();
#if USE_DESKTOP
        if (capture && lv_tick_elaps(started) >= 500) { screenshot(app.root, capture); break; }
#else
        lv_tick_inc(5);
#endif
        usleep(5000);
    }
    close(wire);
    return 0;
}
