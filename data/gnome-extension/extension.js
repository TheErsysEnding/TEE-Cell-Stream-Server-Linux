// Mutter hands a fullscreen window straight to the monitor (direct scanout) and then stops compositing
// it. GNOME's ScreenCast has nothing left to copy, so the stream freezes on its last picture while
// sound and input keep running - exactly what a PS3 sees as "the picture stopped" (mutter#3074, #3903).
//
// The flag that turns that off is a runtime one, so this extension sets it while the streaming server
// is running and takes it back the moment the server goes away. Direct scanout is worth having when
// nobody is streaming, which is why this is tied to the server's D-Bus name rather than left on.

import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const SERVER_BUS_NAME = 'de.tee.CellStreamServer';
const FLAG = Meta.DebugPaintFlag.DISABLE_DIRECT_SCANOUT;

export default class TeeCellStreamScanout extends Extension {
    enable() {
        this._applied = false;
        this._watch = Gio.bus_watch_name(
            Gio.BusType.SESSION, SERVER_BUS_NAME, Gio.BusNameWatcherFlags.NONE,
            () => this._setDisabled(true),
            () => this._setDisabled(false));
        console.log('tee-cell-stream-scanout: warte auf ' + SERVER_BUS_NAME);
    }

    disable() {
        if (this._watch) {
            Gio.bus_unwatch_name(this._watch);
            this._watch = null;
        }
        this._setDisabled(false);
    }

    _setDisabled(disableScanout) {
        if (disableScanout === this._applied)
            return;
        if (disableScanout)
            Meta.add_debug_paint_flag(FLAG);
        else
            Meta.remove_debug_paint_flag(FLAG);
        this._applied = disableScanout;
        console.log('tee-cell-stream-scanout: Direktdurchreichung ' + (disableScanout ? 'AUS' : 'AN'));
    }
}
