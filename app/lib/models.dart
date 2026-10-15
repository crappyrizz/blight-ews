// The data the app shows, parsed from the backend's JSON.
//
// Parsing is forgiving: a field the backend leaves out or sets to null
// becomes "not known" in the app, never a crash. Missing data is shown as
// missing, never as "low risk" (the project's missing-data rule).

class Farmer {
  Farmer({required this.farmerId, required this.name, required this.phoneNumber, this.farmId});

  final String farmerId;
  final String name;
  final String phoneNumber;
  final String? farmId;

  factory Farmer.fromJson(Map<String, dynamic> j) => Farmer(
        farmerId: j['farmer_id'] as String,
        name: j['name'] as String,
        phoneNumber: j['phone_number'] as String,
        farmId: j['farm_id'] as String?,
      );
}

class SensorNode {
  SensorNode({required this.nodeId, required this.location});

  final String nodeId;
  final String location;

  factory SensorNode.fromJson(Map<String, dynamic> j) =>
      SensorNode(nodeId: j['node_id'] as String, location: (j['location'] as String?) ?? 'My plot');
}

/// low / medium / high, or unknown when there is no recent judged data.
enum RiskLevel { low, medium, high, unknown }

RiskLevel parseRiskLevel(String? value) => switch (value) {
      'low' => RiskLevel.low,
      'medium' => RiskLevel.medium,
      'high' => RiskLevel.high,
      _ => RiskLevel.unknown,
    };

/// Words for the farmer. "medium" is shown as MODERATE, as in the wireframe.
String riskLabel(RiskLevel level) => switch (level) {
      RiskLevel.low => 'LOW RISK',
      RiskLevel.medium => 'MODERATE RISK',
      RiskLevel.high => 'HIGH RISK',
      RiskLevel.unknown => 'RISK UNKNOWN',
    };

/// "just now", "12 min ago", "3 hours ago", "2 days ago".
String ageText(int? minutes) {
  if (minutes == null) return 'no readings yet';
  if (minutes < 2) return 'just now';
  if (minutes < 60) return '$minutes min ago';
  final hours = minutes ~/ 60;
  if (hours < 48) return '$hours ${hours == 1 ? 'hour' : 'hours'} ago';
  return '${hours ~/ 24} days ago';
}

class LatestReading {
  LatestReading({required this.temperature, required this.humidity, required this.ageMinutes});

  final double temperature;
  final double humidity;
  final int ageMinutes;

  static LatestReading? fromJson(dynamic j) => j is Map<String, dynamic>
      ? LatestReading(
          temperature: (j['temperature'] as num).toDouble(),
          humidity: (j['humidity'] as num).toDouble(),
          ageMinutes: (j['age_minutes'] as num).toInt(),
        )
      : null;
}

class HuttonToday {
  HuttonToday({required this.status, required this.label, this.huttonDay, this.huttonPeriod});

  final String status; // ok | insufficient_data | no_data | not_computed
  final String label; // e.g. "Incomplete data (14 of 24 hours)", ready to show
  final bool? huttonDay;
  final bool? huttonPeriod;

  factory HuttonToday.fromJson(Map<String, dynamic>? j) => HuttonToday(
        status: (j?['status'] as String?) ?? 'not_computed',
        label: (j?['label'] as String?) ?? 'Not computed yet',
        huttonDay: j?['hutton_day'] as bool?,
        huttonPeriod: j?['hutton_period'] as bool?,
      );

  /// One plain line about today's weather against the Hutton rule.
  String get explanation {
    if (status != 'ok') return label; // the backend already says what is missing
    if (huttonPeriod == true) return 'Blight weather two days running (Hutton period)';
    if (huttonDay == true) return 'Blight weather today (Hutton day)';
    return 'Today has not had blight weather';
  }
}

class Outlook {
  Outlook({required this.probability, required this.level, required this.issuedFrom, required this.covers});

  final double probability;
  final RiskLevel level;
  final DateTime? issuedFrom; // the day whose readings made the forecast
  final List<DateTime> covers; // the days it is about

  static Outlook? fromJson(dynamic j) => j is Map<String, dynamic>
      ? Outlook(
          probability: (j['probability'] as num).toDouble(),
          level: parseRiskLevel(j['risk_level'] as String?),
          issuedFrom: DateTime.tryParse((j['issued_from'] ?? '').toString()),
          covers: ((j['covers'] as List?) ?? const [])
              .map((d) => DateTime.tryParse(d.toString()))
              .whereType<DateTime>()
              .toList(),
        )
      : null;

  /// Still about today or later? A forecast for days already past must not
  /// be shown as "next 24 hours" (e.g. when the sensor has gone quiet).
  bool isCurrent(DateTime today) {
    if (covers.isEmpty) return false;
    final day = DateTime(today.year, today.month, today.day);
    return !covers.last.isBefore(day);
  }
}

const _months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/// "11 Oct".
String shortDate(DateTime d) => '${d.day} ${_months[d.month - 1]}';

class Dashboard {
  Dashboard({
    required this.level,
    required this.score,
    required this.riskNote,
    required this.summary,
    required this.latest,
    required this.huttonToday,
    required this.outlook24,
    required this.outlook48,
  });

  final RiskLevel level;
  final double? score;
  final String riskNote;
  final String summary;
  final LatestReading? latest;
  final HuttonToday huttonToday;
  final Outlook? outlook24;
  final Outlook? outlook48;

  factory Dashboard.fromJson(Map<String, dynamic> j) {
    final risk = (j['risk'] as Map<String, dynamic>?) ?? const {};
    final forecast = (j['forecast'] as Map<String, dynamic>?) ?? const {};
    // The neutral prior (no recent judged data) is "unknown", not "moderate".
    final unknown = risk['basis'] == 'neutral_prior';
    return Dashboard(
      level: unknown ? RiskLevel.unknown : parseRiskLevel(risk['risk_level'] as String?),
      score: unknown ? null : (risk['score'] as num?)?.toDouble(),
      riskNote: (risk['note'] as String?) ?? '',
      summary: (j['summary'] as String?) ?? '',
      latest: LatestReading.fromJson(j['latest_reading']),
      huttonToday: HuttonToday.fromJson(j['hutton_today'] as Map<String, dynamic>?),
      outlook24: Outlook.fromJson(forecast['p24']),
      outlook48: Outlook.fromJson(forecast['p48']),
    );
  }
}
