import AppKit

@MainActor
enum BenchWindow {
    /// Fixed size on a fixed screen, never fullscreen. Window size and display
    /// scaling move GPU load more than any codec difference, so they are pinned.
    static func make(size: CGSize, displayIndex: Int, hosting view: NSView) -> NSWindow {
        let screen = NSScreen.screens.indices.contains(displayIndex) ? NSScreen.screens[displayIndex] : NSScreen.main!
        let origin = CGPoint(x: screen.frame.midX - size.width / 2, y: screen.frame.midY - size.height / 2)
        let window = NSWindow(contentRect: CGRect(origin: origin, size: size),
                              styleMask: [.titled], backing: .buffered, defer: false, screen: screen)
        window.contentView = view
        view.frame = CGRect(origin: .zero, size: size)
        view.autoresizingMask = [.width, .height]
        window.makeKeyAndOrderFront(nil)
        return window
    }
}
