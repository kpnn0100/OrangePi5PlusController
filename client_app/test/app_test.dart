import 'package:flutter_test/flutter_test.dart';

import 'package:arstro_remote_client/main.dart';

void main() {
  // On the test host (not Android) the page says so instead of talking to the phone service.
  testWidgets('the app opens on its phone page', (tester) async {
    await tester.pumpWidget(const ClientApp());
    await tester.pump();
    expect(find.text('Arstro Remote Client'), findsOneWidget);
    expect(find.text('Only an Android phone can be shared.'), findsOneWidget);
  });
}
