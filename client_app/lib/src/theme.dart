import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// The Arstro look (docs/design.md): dark, flat, warm near-black surfaces, off-white text, 1 px
/// hairlines and one off-white primary button per screen. The same tokens as the web UIs' `:root`
/// (relay/arstro_relay/static/style.css); change docs/design.md first, then every copy of this file
/// (devices_app, client_app, app).
abstract final class Arstro {
  static const bg = Color(0xFF0C0C0B);
  static const surface = Color(0xFF131312);
  static const surface2 = Color(0xFF1A1A18);
  static const surface3 = Color(0xFF222220);
  static const line = Color(0xFF242422);
  static const line2 = Color(0xFF32322F);
  static const text = Color(0xFFEDEDEA);
  static const text2 = Color(0xFFB2B0A9);
  static const muted = Color(0xFF868379);
  static const primary = text;
  static const primaryHover = Color(0xFFD9D8D4);
  static const onPrimary = bg;
  static const ok = Color(0xFF7CC495);
  static const okSoft = Color(0x1F7CC495);
  static const warn = Color(0xFFDDB764);
  static const warnSoft = Color(0x1FDDB764);
  static const err = Color(0xFFEC7E76);
  static const errSoft = Color(0x1FEC7E76);
  static const rec = Color(0xFFF0645A);
  static const info = Color(0xFF8FB3E0);
  static const ring = Color(0x1FEDEDEA);

  static const font = 'Geist';
  static const mono = 'GeistMono';

  /// Radii: cards 10, buttons and inputs 8, chips 6, sheets and dialogs 12.
  static const rCard = 10.0;
  static const rButton = 8.0;
  static const rChip = 6.0;
  static const rSheet = 12.0;

  /// Small uppercase section label: 11.5 px, 500, letter-spacing .06em, muted (the caller upper-cases).
  static const overline =
      TextStyle(fontFamily: font, fontSize: 11.5, fontWeight: FontWeight.w500, letterSpacing: 0.7, height: 1.3, color: muted);

  /// Meta data under a title: 12.5 px, muted.
  static const meta = TextStyle(fontFamily: font, fontSize: 12.5, fontWeight: FontWeight.w400, height: 1.45, color: muted);

  /// Ids, ports, keys, versions: Geist Mono with numbers that line up.
  static TextStyle monoStyle({double size = 13, Color color = text, FontWeight weight = FontWeight.w400}) => TextStyle(
      fontFamily: mono, fontSize: size, color: color, fontWeight: weight, height: 1.4,
      fontFeatures: const [FontFeature.tabularFigures()]);

  /// A compact button (36 px) for rows and card titles: `style: Arstro.small` on an Outlined/TextButton.
  static const small = ButtonStyle(
    minimumSize: WidgetStatePropertyAll(Size(0, 36)),
    padding: WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 12)),
    textStyle: WidgetStatePropertyAll(TextStyle(fontFamily: font, fontSize: 13.5, fontWeight: FontWeight.w500, height: 1.2)),
    tapTargetSize: MaterialTapTargetSize.shrinkWrap,
    visualDensity: VisualDensity.compact,
  );

  /// The Geist licence (SIL OFL 1.1, fonts/LICENSE.Geist.txt) with the app's other licences.
  static void registerFontLicence() {
    LicenseRegistry.addLicense(() async* {
      yield LicenseEntryWithLineBreaks(const ['Geist', 'Geist Mono'], await rootBundle.loadString('fonts/LICENSE.Geist.txt'));
    });
  }

  static ThemeData theme() {
    const scheme = ColorScheme(
      brightness: Brightness.dark,
      primary: primary,
      onPrimary: onPrimary,
      primaryContainer: surface3,
      onPrimaryContainer: text,
      secondary: text2,
      onSecondary: bg,
      secondaryContainer: surface3,
      onSecondaryContainer: text,
      tertiary: warn,
      onTertiary: bg,
      tertiaryContainer: warnSoft,
      onTertiaryContainer: warn,
      error: err,
      onError: bg,
      errorContainer: errSoft,
      onErrorContainer: err,
      surface: bg,
      onSurface: text,
      onSurfaceVariant: text2,
      surfaceDim: bg,
      surfaceBright: surface3,
      surfaceContainerLowest: bg,
      surfaceContainerLow: surface,
      surfaceContainer: surface,
      surfaceContainerHigh: surface2,
      surfaceContainerHighest: surface3,
      outline: line2,
      outlineVariant: line,
      shadow: Colors.black,
      scrim: Colors.black,
      inverseSurface: text,
      onInverseSurface: bg,
      inversePrimary: bg,
      surfaceTint: Colors.transparent,
    );

    TextStyle t(double size, FontWeight w, {double height = 1.45, double ls = 0, Color color = text}) =>
        TextStyle(fontFamily: font, fontSize: size, fontWeight: w, height: height, letterSpacing: ls, color: color);
    const w4 = FontWeight.w400, w5 = FontWeight.w500, w6 = FontWeight.w600;
    final textTheme = TextTheme(
      displayLarge: t(48, w6, height: 1.1, ls: -1.2),
      displayMedium: t(40, w6, height: 1.1, ls: -1),
      displaySmall: t(34, w6, height: 1.15, ls: -0.85),
      headlineLarge: t(30, w6, height: 1.15, ls: -0.75),
      headlineMedium: t(27, w6, height: 1.15, ls: -0.65),
      headlineSmall: t(24, w6, height: 1.2, ls: -0.55),
      titleLarge: t(20, w6, height: 1.25, ls: -0.3),
      titleMedium: t(15.5, w5, height: 1.35, ls: -0.1),
      titleSmall: t(14, w5, height: 1.35),
      bodyLarge: t(15, w4, height: 1.5),
      bodyMedium: t(14, w4, height: 1.5),
      bodySmall: t(12.5, w4, color: text2),
      labelLarge: t(14, w5, height: 1.3),
      labelMedium: t(12.5, w5, height: 1.3, color: text2),
      labelSmall: t(11.5, w5, height: 1.3, ls: 0.3, color: text2),
    );

    WidgetStateProperty<T> states<T>(T normal, {T? disabled, T? selected, T? pressed}) =>
        WidgetStateProperty.resolveWith((s) {
          if (disabled != null && s.contains(WidgetState.disabled)) return disabled;
          if (selected != null && s.contains(WidgetState.selected)) return selected;
          if (pressed != null && s.contains(WidgetState.pressed)) return pressed;
          return normal;
        });
    const buttonShape = RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(rButton)));
    final buttonText = t(14, w5, height: 1.2);
    const hover = Color(0x0FEDEDEA);
    const press = Color(0x1AEDEDEA);
    final overlay = WidgetStateProperty.resolveWith<Color?>((s) {
      if (s.contains(WidgetState.pressed)) return press;
      if (s.contains(WidgetState.hovered) || s.contains(WidgetState.focused)) return hover;
      return null;
    });
    final inputBorder = OutlineInputBorder(
        borderRadius: BorderRadius.circular(rButton), borderSide: const BorderSide(color: line2));

    return ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: scheme,
      fontFamily: font,
      textTheme: textTheme,
      scaffoldBackgroundColor: bg,
      canvasColor: surface,
      dividerColor: line,
      disabledColor: muted,
      hintColor: muted,
      splashFactory: InkRipple.splashFactory,
      splashColor: press,
      highlightColor: hover,
      hoverColor: hover,
      focusColor: ring,
      visualDensity: VisualDensity.standard,
      iconTheme: const IconThemeData(color: text2),
      appBarTheme: AppBarThemeData(
        backgroundColor: bg,
        foregroundColor: text,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleSpacing: 16,
        titleTextStyle: t(17, w6, height: 1.2, ls: -0.2),
        iconTheme: const IconThemeData(color: text, size: 22),
        actionsIconTheme: const IconThemeData(color: text2, size: 22),
        shape: const Border(bottom: BorderSide(color: line)),
        systemOverlayStyle: const SystemUiOverlayStyle(
          statusBarColor: Colors.transparent,
          statusBarIconBrightness: Brightness.light,
          statusBarBrightness: Brightness.dark,
          systemNavigationBarColor: bg,
          systemNavigationBarIconBrightness: Brightness.light,
        ),
      ),
      cardTheme: const CardThemeData(
        color: surface,
        elevation: 0,
        shadowColor: Colors.transparent,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(rCard)), side: BorderSide(color: line)),
      ),
      // colours from the scheme: FilledButton = primary / on-primary (the one per screen),
      // FilledButton.tonal = surface-3 / text (a quieter filled button)
      filledButtonTheme: FilledButtonThemeData(
        style: ButtonStyle(
          elevation: const WidgetStatePropertyAll(0),
          shape: const WidgetStatePropertyAll(buttonShape),
          minimumSize: const WidgetStatePropertyAll(Size(64, 44)),
          padding: const WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 18)),
          textStyle: WidgetStatePropertyAll(buttonText),
          iconSize: const WidgetStatePropertyAll(18),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: ButtonStyle(
          foregroundColor: states(text, disabled: muted),
          iconColor: states(text2, disabled: muted),
          overlayColor: overlay,
          side: states(const BorderSide(color: line2), disabled: const BorderSide(color: line)),
          shape: const WidgetStatePropertyAll(buttonShape),
          minimumSize: const WidgetStatePropertyAll(Size(64, 44)),
          padding: const WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 16)),
          textStyle: WidgetStatePropertyAll(buttonText),
          iconSize: const WidgetStatePropertyAll(18),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: ButtonStyle(
          foregroundColor: states(text2, disabled: muted),
          iconColor: states(text2, disabled: muted),
          overlayColor: overlay,
          shape: const WidgetStatePropertyAll(buttonShape),
          minimumSize: const WidgetStatePropertyAll(Size(48, 40)),
          padding: const WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 12)),
          textStyle: WidgetStatePropertyAll(buttonText),
          iconSize: const WidgetStatePropertyAll(18),
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ButtonStyle(
          backgroundColor: states(surface2, disabled: surface),
          foregroundColor: states(text, disabled: muted),
          overlayColor: overlay,
          elevation: const WidgetStatePropertyAll(0),
          side: const WidgetStatePropertyAll(BorderSide(color: line2)),
          shape: const WidgetStatePropertyAll(buttonShape),
          minimumSize: const WidgetStatePropertyAll(Size(64, 44)),
          textStyle: WidgetStatePropertyAll(buttonText),
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: ButtonStyle(
          foregroundColor: states(text2, disabled: line2, selected: text),
          overlayColor: overlay,
        ),
      ),
      floatingActionButtonTheme: const FloatingActionButtonThemeData(
        backgroundColor: primary,
        foregroundColor: onPrimary,
        elevation: 0,
        focusElevation: 0,
        hoverElevation: 0,
        highlightElevation: 0,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(rCard))),
      ),
      inputDecorationTheme: InputDecorationThemeData(
        filled: true,
        fillColor: surface2,
        contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
        border: inputBorder,
        enabledBorder: inputBorder,
        disabledBorder: inputBorder.copyWith(borderSide: const BorderSide(color: line)),
        focusedBorder: inputBorder.copyWith(borderSide: const BorderSide(color: text2)),
        errorBorder: inputBorder.copyWith(borderSide: const BorderSide(color: err)),
        focusedErrorBorder: inputBorder.copyWith(borderSide: const BorderSide(color: err)),
        hintStyle: t(14.5, w4, color: muted),
        labelStyle: t(14, w4, color: text2),
        floatingLabelStyle: t(14, w5, color: text2),
        helperStyle: t(12.5, w4, color: muted),
        errorStyle: t(12.5, w4, color: err),
        prefixIconColor: muted,
        suffixIconColor: text2,
      ),
      textSelectionTheme: const TextSelectionThemeData(
          cursorColor: text, selectionColor: Color(0x2EEDEDEA), selectionHandleColor: text),
      dividerTheme: const DividerThemeData(color: line, thickness: 1, space: 1),
      listTileTheme: ListTileThemeData(
        iconColor: text2,
        titleTextStyle: t(15, w4, height: 1.35),
        subtitleTextStyle: t(13, w4, color: muted),
        leadingAndTrailingTextStyle: t(13, w4, color: muted),
        contentPadding: const EdgeInsets.symmetric(horizontal: 16),
        horizontalTitleGap: 12,
        minLeadingWidth: 24,
        selectedColor: text,
        selectedTileColor: surface3,
      ),
      switchTheme: SwitchThemeData(
        thumbColor: states(muted, disabled: line2, selected: onPrimary),
        trackColor: states(surface3, disabled: surface2, selected: primary),
        trackOutlineColor: states(line2, selected: Colors.transparent),
        trackOutlineWidth: const WidgetStatePropertyAll(1),
        overlayColor: overlay,
      ),
      checkboxTheme: CheckboxThemeData(
        fillColor: states(Colors.transparent, selected: primary),
        checkColor: const WidgetStatePropertyAll(onPrimary),
        side: const BorderSide(color: line2, width: 1.5),
        shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(4))),
        overlayColor: overlay,
      ),
      radioTheme: RadioThemeData(fillColor: states(muted, selected: primary), overlayColor: overlay),
      progressIndicatorTheme: const ProgressIndicatorThemeData(
          color: text, linearTrackColor: surface3, circularTrackColor: Colors.transparent, linearMinHeight: 2),
      sliderTheme: const SliderThemeData(
        activeTrackColor: primary,
        inactiveTrackColor: surface3,
        thumbColor: primary,
        overlayColor: ring,
        valueIndicatorColor: surface3,
        activeTickMarkColor: onPrimary,
        inactiveTickMarkColor: line2,
      ),
      chipTheme: ChipThemeData(
        backgroundColor: surface3,
        selectedColor: surface3,
        disabledColor: surface2,
        side: BorderSide.none,
        shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(rChip))),
        labelStyle: t(12.5, w5, height: 1.2),
        padding: const EdgeInsets.symmetric(horizontal: 4),
        elevation: 0,
        pressElevation: 0,
        iconTheme: const IconThemeData(color: text2, size: 16),
        checkmarkColor: text,
      ),
      snackBarTheme: SnackBarThemeData(
        backgroundColor: surface3,
        contentTextStyle: t(14, w4, height: 1.4),
        actionTextColor: text,
        behavior: SnackBarBehavior.floating,
        elevation: 4,
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(rButton)), side: BorderSide(color: line2)),
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        barrierColor: const Color(0x99000000),
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(rSheet)), side: BorderSide(color: line)),
        titleTextStyle: t(17, w6, height: 1.3, ls: -0.2),
        contentTextStyle: t(14, w4, height: 1.5, color: text2),
      ),
      bottomSheetTheme: const BottomSheetThemeData(
        backgroundColor: surface,
        modalBackgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        modalElevation: 0,
        modalBarrierColor: Color(0x99000000),
        showDragHandle: true,
        dragHandleColor: line2,
        dragHandleSize: Size(36, 4),
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.vertical(top: Radius.circular(rSheet)), side: BorderSide(color: line)),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: surface2,
        surfaceTintColor: Colors.transparent,
        elevation: 8,
        shadowColor: const Color(0x66000000),
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(rButton)), side: BorderSide(color: line2)),
        textStyle: t(14, w4),
        labelTextStyle: WidgetStatePropertyAll(t(14, w4)),
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
            color: surface3, borderRadius: BorderRadius.circular(rChip), border: Border.all(color: line2)),
        textStyle: t(12.5, w4, height: 1.3),
      ),
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        height: 64,
        indicatorColor: surface3,
        indicatorShape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(rButton))),
        labelTextStyle: WidgetStateProperty.resolveWith((s) =>
            t(12, w5, height: 1.2, color: s.contains(WidgetState.selected) ? text : muted)),
        iconTheme: WidgetStateProperty.resolveWith((s) =>
            IconThemeData(size: 22, color: s.contains(WidgetState.selected) ? text : text2)),
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: surface,
        elevation: 0,
        indicatorColor: surface3,
        indicatorShape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(rButton))),
        selectedIconTheme: const IconThemeData(color: text, size: 22),
        unselectedIconTheme: const IconThemeData(color: text2, size: 22),
        selectedLabelTextStyle: t(12, w5, height: 1.2),
        unselectedLabelTextStyle: t(12, w5, height: 1.2, color: muted),
      ),
      drawerTheme: const DrawerThemeData(
        backgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        shape: RoundedRectangleBorder(),
      ),
      tabBarTheme: TabBarThemeData(
        labelColor: text,
        unselectedLabelColor: muted,
        dividerColor: line,
        indicatorColor: primary,
        indicator: const UnderlineTabIndicator(borderSide: BorderSide(color: primary, width: 2)),
        labelStyle: t(14, w5, height: 1.2),
        unselectedLabelStyle: t(14, w5, height: 1.2),
        overlayColor: overlay,
      ),
      segmentedButtonTheme: SegmentedButtonThemeData(
        style: ButtonStyle(
          backgroundColor: states(Colors.transparent, selected: surface3),
          foregroundColor: states(text2, disabled: muted, selected: text),
          iconColor: states(text2, disabled: muted, selected: text),
          overlayColor: overlay,
          side: const WidgetStatePropertyAll(BorderSide(color: line2)),
          shape: const WidgetStatePropertyAll(buttonShape),
          textStyle: WidgetStatePropertyAll(t(13.5, w5, height: 1.2)),
        ),
      ),
      expansionTileTheme: const ExpansionTileThemeData(
        iconColor: text2,
        collapsedIconColor: muted,
        textColor: text,
        collapsedTextColor: text,
        shape: Border(),
        collapsedShape: Border(),
      ),
      badgeTheme: const BadgeThemeData(backgroundColor: err, textColor: bg),
      scrollbarTheme: const ScrollbarThemeData(thumbColor: WidgetStatePropertyAll(line2)),
    );
  }
}
