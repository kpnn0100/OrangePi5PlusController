import 'package:flutter_test/flutter_test.dart';

import 'package:arstro_remote_client/main.dart';
import 'package:arstro_remote_client/src/phone_api.dart';

void main() {
  // On the test host (not Android) the page says so instead of talking to the phone service.
  testWidgets('the app opens on its phone page', (tester) async {
    await tester.pumpWidget(const ClientApp());
    await tester.pump();
    expect(find.text('Arstro Remote Client'), findsOneWidget);
    expect(find.text('Only an Android phone can be shared.'), findsOneWidget);
  });

  // a phone maker whose battery manager stops apps: the page says where to allow it
  test('makers that stop apps in the background get a hint', () {
    PhoneState of(String maker) => PhoneState.fromMap({'manufacturer': maker, 'awake': true});
    expect(of('TECNO').makerHint, contains('Phone Master'));
    expect(of('Xiaomi').makerHint, contains('auto-start'));
    expect(of('Google').makerHint, isNull);
    expect(of('Google').awake, isTrue);
    expect(PhoneState.fromMap({}).awake, isTrue, reason: 'on unless turned off');
  });
}
