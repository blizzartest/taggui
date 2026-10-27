from PySide6.QtGui import QColor, QPalette

THEMES = ('System', 'Light', 'Dark', 'OLED Black')

THEME_COLORS = {
    'Light': {
        QPalette.ColorRole.Window: '#f0f0f0',
        QPalette.ColorRole.WindowText: '#000000',
        QPalette.ColorRole.Base: '#ffffff',
        QPalette.ColorRole.AlternateBase: '#e4e4e4',
        QPalette.ColorRole.ToolTipBase: '#ffffdc',
        QPalette.ColorRole.ToolTipText: '#000000',
        QPalette.ColorRole.Text: '#000000',
        QPalette.ColorRole.Button: '#f0f0f0',
        QPalette.ColorRole.ButtonText: '#000000',
        QPalette.ColorRole.Highlight: '#0078d7',
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.Link: '#0078d7',
        QPalette.ColorRole.PlaceholderText: '#808080'
    },
    'Dark': {
        QPalette.ColorRole.Window: '#353535',
        QPalette.ColorRole.WindowText: '#ffffff',
        QPalette.ColorRole.Base: '#2b2b2b',
        QPalette.ColorRole.AlternateBase: '#3a3a3a',
        QPalette.ColorRole.ToolTipBase: '#353535',
        QPalette.ColorRole.ToolTipText: '#ffffff',
        QPalette.ColorRole.Text: '#ffffff',
        QPalette.ColorRole.Button: '#3a3a3a',
        QPalette.ColorRole.ButtonText: '#ffffff',
        QPalette.ColorRole.Highlight: '#4c94ff',
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.Link: '#4c94ff',
        QPalette.ColorRole.PlaceholderText: '#8f8f8f'
    },
    'OLED Black': {
        QPalette.ColorRole.Window: '#000000',
        QPalette.ColorRole.WindowText: '#ffffff',
        QPalette.ColorRole.Base: '#000000',
        QPalette.ColorRole.AlternateBase: '#0d0d0d',
        QPalette.ColorRole.ToolTipBase: '#000000',
        QPalette.ColorRole.ToolTipText: '#ffffff',
        QPalette.ColorRole.Text: '#ffffff',
        QPalette.ColorRole.Button: '#0a0a0a',
        QPalette.ColorRole.ButtonText: '#ffffff',
        QPalette.ColorRole.Highlight: '#4c94ff',
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.Link: '#4c94ff',
        QPalette.ColorRole.PlaceholderText: '#808080'
    }
}

TEXT_ROLES = (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
              QPalette.ColorRole.ButtonText, QPalette.ColorRole.Link,
              QPalette.ColorRole.PlaceholderText)


def apply_theme(app, theme: str):
    """Apply a theme to the application. The System theme keeps the style's
    standard palette so that the operating system theme is followed."""
    palette = QPalette(app.style().standardPalette())
    colors = THEME_COLORS.get(theme)
    if colors is not None:
        for group in (QPalette.ColorGroup.Active,
                      QPalette.ColorGroup.Inactive):
            for role, color in colors.items():
                palette.setColor(group, role, QColor(color))
        for role, color in colors.items():
            if role in TEXT_ROLES:
                continue
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color))
        disabled_text_color = (QColor('#909090')
                               if theme == 'Light' else QColor('#707070'))
        for role in TEXT_ROLES:
            palette.setColor(QPalette.ColorGroup.Disabled, role,
                             disabled_text_color)
    app.setPalette(palette)
