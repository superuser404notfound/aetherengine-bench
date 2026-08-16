import AppKit

@MainActor
enum BenchWindow {
    /// The window's real geometry, in points and in pixels. GPU cost scales with
    /// pixels, and a size requested in points on a Retina display is neither the
    /// pixel count nor necessarily what the window ends up with, because macOS
    /// constrains a window to the screen's visible frame. So this is measured
    /// after the window exists and reported, never assumed.
    struct Geometry: Codable, Equatable {
        let pointWidth: Int
        let pointHeight: Int
        let pixelWidth: Int
        let pixelHeight: Int
        let backingScale: Double
    }

    private(set) static var actualGeometry: Geometry?

    /// Fixed size on a fixed screen, never fullscreen. Window size and display
    /// scaling move GPU load more than any codec difference, so they are pinned.
    static func make(size: CGSize, displayIndex: Int, hosting view: NSView) -> NSWindow {
        // No screen means no window, and no window means nothing worth measuring.
        guard let screen = NSScreen.screens.indices.contains(displayIndex)
            ? NSScreen.screens[displayIndex] : NSScreen.main else {
            fatalError("no display available; the benchmark measures on-screen playback")
        }
        let origin = CGPoint(x: screen.frame.midX - size.width / 2, y: screen.frame.midY - size.height / 2)
        let window = NSWindow(contentRect: CGRect(origin: origin, size: size),
                              styleMask: [.titled], backing: .buffered, defer: false, screen: screen)
        window.contentView = view
        view.frame = CGRect(origin: .zero, size: size)
        view.autoresizingMask = [.width, .height]
        window.makeKeyAndOrderFront(nil)

        let backing = view.convertToBacking(view.bounds).size
        actualGeometry = Geometry(pointWidth: Int(view.bounds.width.rounded()),
                                  pointHeight: Int(view.bounds.height.rounded()),
                                  pixelWidth: Int(backing.width.rounded()),
                                  pixelHeight: Int(backing.height.rounded()),
                                  backingScale: Double(window.backingScaleFactor))
        let note = "bench window: \(Int(view.bounds.width.rounded()))x\(Int(view.bounds.height.rounded())) pt, "
            + "\(Int(backing.width.rounded()))x\(Int(backing.height.rounded())) px, "
            + "scale \(window.backingScaleFactor)\n"
        FileHandle.standardError.write(Data(note.utf8))
        return window
    }
}
