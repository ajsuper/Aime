#include "sqlite3.h"
#include <iostream>
#include <vector>
#include <sstream>
#include <algorithm>
#include <string>
#include <ctime>
#include <fstream>
#include <filesystem>
#include <cstdlib>
#include <cctype>
#include <mutex>
#include <unordered_map>

#include "../include/crow_all.h"

// Root directory under which every user's database lives. Set from
// $HOME/.local/share/aime-assistant/database (or argv[1]) in main().
// Each user gets:
//     <g_root>/users/<user_id>/database.sql
//     <g_root>/users/<user_id>/topics/<topic_id>_<title>.md
// Auth state and the Flask session secret live one level up at <g_root>/.
static std::string g_root;

// Per-user sqlite handle cache. Crow is multithreaded and sqlite (built in
// the default SERIALIZED mode) is safe to share a connection across threads,
// so we hand out the same handle to whichever worker thread is currently
// servicing that user's request. Never evicted: open handles are cheap and
// the user population for a personal-scale deploy is small.
static std::unordered_map<int, sqlite3*> g_user_dbs;
static std::mutex g_user_dbs_mu;

static std::string userDir(int user_id) {
    return g_root + "/users/" + std::to_string(user_id);
}

static std::string userTopicsDir(int user_id) {
    return userDir(user_id) + "/topics";
}

// ─── Data structures ─────────────────────────────────────────────────────────

struct CalenderEvent {
    int id = -1;
    std::string eventTitle;
    std::string eventSummary;
    std::string eventCategory;
    std::string eventDate;  // DD/MM/YYYY
    std::string eventTime;  // HH:MM
    bool eventArchived = false;
};

struct Date {
    uint day;
    uint month;
    uint year;
};

struct Time {
    uint hour;
    uint minute;
};

struct FilterOptions {
    // Date range — only applied when filterByDate = true
    bool filterByDate = false;
    Date startDate = {1, 1, 1970};
    Date endDate   = {31, 12, 9999};

    // Time range — only applied when filterByTime = true
    bool filterByTime = false;
    Time startTime = {0, 0};
    Time endTime   = {23, 59};

    // Category: set one or the other, not both
    std::string category;              // exact single-category match
    std::vector<std::string> categories; // OR match across multiple categories

    // Full-text keyword search across title and summary (case-insensitive for ASCII)
    std::string keyword;

    enum class ArchivedFilter { All, ActiveOnly, ArchivedOnly } archived = ArchivedFilter::ActiveOnly;
    enum class SortOrder { DateAsc, DateDesc } sortOrder = SortOrder::DateAsc;

    // Number of filtered items.
    int limit = 0; // 0 = unlimited
};

struct TopicFilterOptions {
    // Category: set one or the other, not both
    std::string category;              // exact single-category match
    std::vector<std::string> categories; // OR match across multiple categories

    // Full-text keyword search across title and summary (case-insensitive for ASCII)
    std::string keyword;
    int limit = 0; // 0 = unlimited
};

struct Topic {
    int id = -1;
    std::string topicTitle;
    std::string topicSummary;
    std::string topicCategory;
};

struct EditPatch {
    std::string find;
    std::string replace;
    int occurrence = 0; // 0 = require a unique match; >=1 = explicit 1-based index
};

struct EditResult {
    bool ok = true;
    std::string error;
    int applied = 0;
};

// ─── Calendar open / schema
void openDatabase(char* filename, sqlite3** database) {
    int opened = sqlite3_open(filename, database);
    if (opened) {
        std::cout << "Failed to open calender: " << sqlite3_errmsg(*database) << std::endl;
    } else {
        std::cout << "Opened calender!" << std::endl;
    }
}

void createCalender(sqlite3* database) {
    char* errMsg = nullptr;
    std::string sqlCommand =
        "CREATE TABLE EVENT("
        "ID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "EVENT_TITLE TEXT NOT NULL,"
        "EVENT_SUMMARY TEXT NOT NULL,"
        "EVENT_CATEGORY TEXT NOT NULL,"
        "EVENT_DATE TEXT NOT NULL,"
        "EVENT_TIME TEXT NOT NULL,"
        "EVENT_ARCHIVED TEXT NOT NULL"
        ")";

    int result = sqlite3_exec(database, sqlCommand.c_str(), NULL, 0, &errMsg);
    if (result != SQLITE_OK) {
        std::cout << "Note: createCalender — " << errMsg << std::endl;
    } else {
        std::cout << "Created calender!" << std::endl;
    }
}

void createTopics(sqlite3* database) {
    char* errMsg = nullptr;
    std::string sqlCommand =
        "CREATE TABLE TOPIC("
        "ID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "TOPIC_TITLE TEXT NOT NULL,"
        "TOPIC_SUMMARY TEXT NOT NULL,"
        "TOPIC_CATEGORY TEXT NOT NULL"
        ")"; // Topics will be storn as .md file in database/topics/, topic title will be id__topic_name_no_special_char.md

    int result = sqlite3_exec(database, sqlCommand.c_str(), NULL, 0, &errMsg);
    if (result != SQLITE_OK) {
        std::cout << "Note: createTopics — " << errMsg << std::endl;
    } else {
        std::cout << "Created topics!" << std::endl;
    }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

static std::string sanitizeFileName(const std::string& input) {
    std::string result;
    for (char c : input) {
        if (std::isalnum(c) || c == '_') {
            result += std::tolower(c);
        } else if (c == ' ') {
            result += '_';
        }
    }
    return result;
}

static CalenderEvent rowToEvent(sqlite3_stmt* stmt) {
    CalenderEvent event;
    event.id           = sqlite3_column_int(stmt, 0);
    event.eventTitle   = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
    event.eventSummary = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2));
    event.eventCategory = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 3));
    event.eventDate    = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 4));
    event.eventTime    = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 5));
    std::string archived = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 6));
    event.eventArchived = (archived == "TRUE");
    return event;
}

static Date parseDate(const std::string& dateStr) {
    Date date = {0, 0, 0};
    char sep;
    std::istringstream ss(dateStr);
    ss >> date.day >> sep >> date.month >> sep >> date.year;
    return date;
}

static Time parseTime(const std::string& timeStr) {
    Time t = {0, 0};
    char sep;
    std::istringstream ss(timeStr);
    ss >> t.hour >> sep >> t.minute;
    return t;
}

static int packDate(const Date& d) { return d.year * 10000 + d.month * 100 + d.day; }
static int packTime(const Time& t) { return t.hour * 100 + t.minute; }

static bool dateInRange(const Date& d, const Date& start, const Date& end) {
    return packDate(d) >= packDate(start) && packDate(d) <= packDate(end);
}

static bool timeInRange(const Time& t, const Time& start, const Time& end) {
    return packTime(t) >= packTime(start) && packTime(t) <= packTime(end);
}

// ─── CRUD ─────────────────────────────────────────────────────────────────────

void addEvent(sqlite3* database, CalenderEvent& event) {
    sqlite3_stmt* stmt;
    const std::string sql =
        "INSERT INTO EVENT(EVENT_TITLE, EVENT_SUMMARY, EVENT_CATEGORY, EVENT_DATE, EVENT_TIME, EVENT_ARCHIVED)"
        " VALUES(?, ?, ?, ?, ?, 'FALSE')";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "addEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_text(stmt, 1, event.eventTitle.c_str(),    -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, event.eventSummary.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, event.eventCategory.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 4, event.eventDate.c_str(),     -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 5, event.eventTime.c_str(),     -1, SQLITE_STATIC);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "addEvent failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
    event.id = static_cast<int>(sqlite3_last_insert_rowid(database));
}

CalenderEvent getEvent(sqlite3* database, int eventID) {
    CalenderEvent event;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM EVENT WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "getEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return event;
    }

    sqlite3_bind_int(stmt, 1, eventID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        event = rowToEvent(stmt);
    } else {
        std::cout << "Event " << eventID << " not found." << std::endl;
    }

    sqlite3_finalize(stmt);
    return event;
}

// Updates all fields except ID and archived status.
void updateEvent(sqlite3* database, const CalenderEvent& event) {
    sqlite3_stmt* stmt;
    const std::string sql =
        "UPDATE EVENT SET EVENT_TITLE=?, EVENT_SUMMARY=?, EVENT_CATEGORY=?, EVENT_DATE=?, EVENT_TIME=?"
        " WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "updateEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_text(stmt, 1, event.eventTitle.c_str(),    -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, event.eventSummary.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, event.eventCategory.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 4, event.eventDate.c_str(),     -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 5, event.eventTime.c_str(),     -1, SQLITE_STATIC);
    sqlite3_bind_int (stmt, 6, event.id);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "updateEvent failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
}

void archiveEvent(sqlite3* database, int eventID) {
    sqlite3_stmt* stmt;
    const std::string sql = "UPDATE EVENT SET EVENT_ARCHIVED='TRUE' WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "archiveEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_int(stmt, 1, eventID);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "archiveEvent failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
}

void unarchiveEvent(sqlite3* database, int eventID) {
    sqlite3_stmt* stmt;
    const std::string sql = "UPDATE EVENT SET EVENT_ARCHIVED='FALSE' WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "unarchiveEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_int(stmt, 1, eventID);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "unarchiveEvent failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
}

// ─── Core filter engine ───────────────────────────────────────────────────────

// All filtering goes through here. SQL handles archived/category/keyword; C++ handles
// date and time ranges (since DD/MM/YYYY strings don't sort lexicographically).
std::vector<CalenderEvent> filterEvents(sqlite3* database, const FilterOptions& opts) {
    std::string sql = "SELECT * FROM EVENT";
    std::vector<std::string> conditions;
    std::vector<std::string> params; // positional bind values

    // Archived
    if (opts.archived == FilterOptions::ArchivedFilter::ActiveOnly) {
        conditions.push_back("EVENT_ARCHIVED = 'FALSE'");
    } else if (opts.archived == FilterOptions::ArchivedFilter::ArchivedOnly) {
        conditions.push_back("EVENT_ARCHIVED = 'TRUE'");
    }

    // Single category
    if (!opts.category.empty()) {
        conditions.push_back("EVENT_CATEGORY = ?");
        params.push_back(opts.category);
    } else if (!opts.categories.empty()) {
        // Multi-category OR
        std::string inClause = "EVENT_CATEGORY IN (";
        for (size_t i = 0; i < opts.categories.size(); i++) {
            inClause += (i == 0 ? "?" : ",?");
            params.push_back(opts.categories[i]);
        }
        inClause += ")";
        conditions.push_back(inClause);
    }

    // Keyword — case-insensitive for ASCII via SQLite LIKE
    if (!opts.keyword.empty()) {
        conditions.push_back("(EVENT_TITLE LIKE ? OR EVENT_SUMMARY LIKE ?)");
        std::string like = "%" + opts.keyword + "%";
        params.push_back(like);
        params.push_back(like);
    }

    if (!conditions.empty()) {
        sql += " WHERE ";
        for (size_t i = 0; i < conditions.size(); i++) {
            if (i > 0) sql += " AND ";
            sql += conditions[i];
        }
    }

    sqlite3_stmt* stmt;
    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "filterEvents prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return {};
    }

    for (size_t i = 0; i < params.size(); i++) {
        sqlite3_bind_text(stmt, static_cast<int>(i + 1), params[i].c_str(), -1, SQLITE_TRANSIENT);
    }

    std::vector<CalenderEvent> results;
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        results.push_back(rowToEvent(stmt));
    }
    sqlite3_finalize(stmt);

    // Date range (C++ post-filter)
    if (opts.filterByDate) {
        results.erase(
            std::remove_if(results.begin(), results.end(), [&](const CalenderEvent& e) {
                return !dateInRange(parseDate(e.eventDate), opts.startDate, opts.endDate);
            }),
            results.end()
        );
    }

    // Time range (C++ post-filter)
    if (opts.filterByTime) {
        results.erase(
            std::remove_if(results.begin(), results.end(), [&](const CalenderEvent& e) {
                return !timeInRange(parseTime(e.eventTime), opts.startTime, opts.endTime);
            }),
            results.end()
        );
    }

    // Sort by date (then by time as a tiebreaker, always ascending)
    std::sort(results.begin(), results.end(), [&](const CalenderEvent& a, const CalenderEvent& b) {
        int da = packDate(parseDate(a.eventDate));
        int db = packDate(parseDate(b.eventDate));
        if (da != db) {
            return opts.sortOrder == FilterOptions::SortOrder::DateAsc ? da < db : da > db;
        }
        return packTime(parseTime(a.eventTime)) < packTime(parseTime(b.eventTime));
    });

    if (opts.limit > 0 && static_cast<int>(results.size()) > opts.limit) {
        results.resize(opts.limit);
    }

    return results;
}

// ─── Convenience filter functions ────────────────────────────────────────────

std::vector<CalenderEvent> getEventsByDateRange(sqlite3* database, Date startDate, Date endDate) {
    FilterOptions opts;
    opts.filterByDate = true;
    opts.startDate    = startDate;
    opts.endDate      = endDate;
    opts.archived     = FilterOptions::ArchivedFilter::All;
    return filterEvents(database, opts);
}

std::vector<CalenderEvent> getEventsByTimeRange(sqlite3* database, Time startTime, Time endTime) {
    FilterOptions opts;
    opts.filterByTime = true;
    opts.startTime    = startTime;
    opts.endTime      = endTime;
    opts.archived     = FilterOptions::ArchivedFilter::All;
    return filterEvents(database, opts);
}

std::vector<CalenderEvent> getEventsByCategory(sqlite3* database, const std::string& category) {
    FilterOptions opts;
    opts.category = category;
    opts.archived = FilterOptions::ArchivedFilter::All;
    return filterEvents(database, opts);
}

// Case-insensitive keyword search across title and summary.
std::vector<CalenderEvent> searchEvents(sqlite3* database, const std::string& keyword) {
    FilterOptions opts;
    opts.keyword  = keyword;
    opts.archived = FilterOptions::ArchivedFilter::All;
    return filterEvents(database, opts);
}

std::vector<CalenderEvent> getActiveEvents(sqlite3* database) {
    FilterOptions opts;
    opts.archived = FilterOptions::ArchivedFilter::ActiveOnly;
    return filterEvents(database, opts);
}

std::vector<CalenderEvent> getArchivedEvents(sqlite3* database) {
    FilterOptions opts;
    opts.archived = FilterOptions::ArchivedFilter::ArchivedOnly;
    return filterEvents(database, opts);
}

// ─── Topic filter engine ─────────────────────────────────────────────────────
Topic rowToTopic(sqlite3_stmt* stmt) {
    Topic topic;
    topic.id = sqlite3_column_int(stmt, 0);
    topic.topicTitle = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
    topic.topicSummary = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2));
    topic.topicCategory = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 3));
    return topic;
}


void addTopic(sqlite3* database, int user_id, Topic& topic) {
    sqlite3_stmt* stmt;
    const std::string sql =
        "INSERT INTO TOPIC(TOPIC_TITLE, TOPIC_SUMMARY, TOPIC_CATEGORY)"
        " VALUES(?, ?, ?)";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "addTopic prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_text(stmt, 1, topic.topicTitle.c_str(),    -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, topic.topicSummary.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, topic.topicCategory.c_str(), -1, SQLITE_STATIC);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "addTopic failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
    topic.id = static_cast<int>(sqlite3_last_insert_rowid(database));

    std::filesystem::create_directories(userTopicsDir(user_id));
    std::string fileName = userTopicsDir(user_id) + "/" + std::to_string(topic.id) + "_" + sanitizeFileName(topic.topicTitle) + ".md";
    std::ofstream topicFile(fileName);
    if (!topicFile.is_open()) {
        std::cout << "Failed to create topic file: " << fileName << std::endl;
        return;
    }

    topicFile << "# " << topic.topicTitle << "\n\n"
              << "**Category:** " << topic.topicCategory << "\n\n"
              << "**Summary:** " << topic.topicSummary << "\n\n"
              << "---\n\n"
              << "## Content\n\n";
    topicFile.close();
}

// Updates topic summary and category. Title is intentionally not updated, since
// the on-disk filename is derived from the title and renaming the .md file is
// out of scope for this endpoint.
void updateTopic(sqlite3* database, const Topic& topic) {
    sqlite3_stmt* stmt;
    const std::string sql =
        "UPDATE TOPIC SET TOPIC_SUMMARY=?, TOPIC_CATEGORY=? WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "updateTopic prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_text(stmt, 1, topic.topicSummary.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, topic.topicCategory.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_int (stmt, 3, topic.id);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "updateTopic failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
}

Topic getTopic(sqlite3* database, int topicID) {
    Topic topic;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM TOPIC WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "getTopic prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return topic;
    }

    sqlite3_bind_int(stmt, 1, topicID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        topic = rowToTopic(stmt);
    }

    sqlite3_finalize(stmt);
    return topic;
}

std::string getTopicContents(sqlite3* database, int user_id, int topicID) {
    Topic topic;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM TOPIC WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "getTopicContents prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return "";
    }

    sqlite3_bind_int(stmt, 1, topicID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        topic = rowToTopic(stmt);
    } else {
        std::cout << "Topic " << topicID << " not found." << std::endl;
        sqlite3_finalize(stmt);
        return "";
    }

    sqlite3_finalize(stmt);

    std::string fileName = userTopicsDir(user_id) + "/" + std::to_string(topic.id) + "_" + sanitizeFileName(topic.topicTitle) + ".md";
    std::ifstream topicFile(fileName);
    if (!topicFile.is_open()) {
        std::cout << "Failed to open topic file: " << fileName << std::endl;
        return "";
    }

    std::string content((std::istreambuf_iterator<char>(topicFile)),
                        std::istreambuf_iterator<char>());
    topicFile.close();
    return content;
}

void replaceTopicContents(sqlite3* database, int user_id, int topicID, const std::string& newContent) {
    Topic topic;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM TOPIC WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "replaceTopicContents prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_int(stmt, 1, topicID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        topic = rowToTopic(stmt);
    } else {
        std::cout << "Topic " << topicID << " not found." << std::endl;
        sqlite3_finalize(stmt);
        return;
    }

    sqlite3_finalize(stmt);

    std::string fileName = userTopicsDir(user_id) + "/" + std::to_string(topic.id) + "_" + sanitizeFileName(topic.topicTitle) + ".md";
    std::ofstream topicFile(fileName);
    if (!topicFile.is_open()) {
        std::cout << "Failed to open topic file for writing: " << fileName << std::endl;
        return;
    }

    topicFile << newContent;
    topicFile.close();
}

EditResult editTopicContents(sqlite3* database, int user_id, int topicID, const std::vector<EditPatch>& patches) {
    EditResult result;
    Topic topic;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM TOPIC WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        result.ok = false;
        result.error = "DB prepare failed";
        return result;
    }
    sqlite3_bind_int(stmt, 1, topicID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        topic = rowToTopic(stmt);
    } else {
        sqlite3_finalize(stmt);
        result.ok = false;
        result.error = "Topic not found.";
        return result;
    }
    sqlite3_finalize(stmt);

    std::string fileName = userTopicsDir(user_id) + "/" + std::to_string(topic.id) + "_" + sanitizeFileName(topic.topicTitle) + ".md";
    std::ifstream in(fileName);
    if (!in.is_open()) {
        result.ok = false;
        result.error = "Failed to open topic file: " + fileName;
        return result;
    }
    std::string content((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    in.close();

    for (size_t i = 0; i < patches.size(); ++i) {
        const EditPatch& p = patches[i];
        if (p.find.empty()) {
            result.ok = false;
            result.error = "Patch " + std::to_string(i) + ": 'find' string is empty.";
            return result;
        }

        std::vector<size_t> positions;
        size_t pos = 0;
        while ((pos = content.find(p.find, pos)) != std::string::npos) {
            positions.push_back(pos);
            pos += p.find.size();
        }

        if (positions.empty()) {
            result.ok = false;
            result.error = "Patch " + std::to_string(i) + ": 'find' string not found. Verify exact text including whitespace and newlines.";
            return result;
        }

        size_t targetPos;
        if (p.occurrence > 0) {
            if (static_cast<size_t>(p.occurrence) > positions.size()) {
                result.ok = false;
                result.error = "Patch " + std::to_string(i) + ": requested occurrence " +
                               std::to_string(p.occurrence) + " but only " +
                               std::to_string(positions.size()) + " match(es) found.";
                return result;
            }
            targetPos = positions[p.occurrence - 1];
        } else {
            if (positions.size() > 1) {
                result.ok = false;
                result.error = "Patch " + std::to_string(i) + ": 'find' string matched " +
                               std::to_string(positions.size()) +
                               " times. Widen 'find' with surrounding context to make it unique, or set 'occurrence'.";
                return result;
            }
            targetPos = positions[0];
        }

        content.replace(targetPos, p.find.size(), p.replace);
        result.applied++;
    }

    std::ofstream out(fileName);
    if (!out.is_open()) {
        result.ok = false;
        result.error = "Failed to open topic file for writing: " + fileName;
        return result;
    }
    out << content;
    out.close();

    return result;
}

std::vector<Topic> filterTopics(sqlite3* database, const TopicFilterOptions& opts) {
    std::string sql = "SELECT * FROM TOPIC";
    std::vector<std::string> conditions;
    std::vector<std::string> params;

    if (!opts.category.empty()) {
        conditions.push_back("TOPIC_CATEGORY = ?");
        params.push_back(opts.category);
    } else if (!opts.categories.empty()) {
        std::string inClause = "TOPIC_CATEGORY IN (";
        for (size_t i = 0; i < opts.categories.size(); i++) {
            inClause += (i == 0 ? "?" : ",?");
            params.push_back(opts.categories[i]);
        }
        inClause += ")";
        conditions.push_back(inClause);
    }

    if (!opts.keyword.empty()) {
        conditions.push_back("(TOPIC_TITLE LIKE ? OR TOPIC_SUMMARY LIKE ?)");
        std::string like = "%" + opts.keyword + "%";
        params.push_back(like);
        params.push_back(like);
    }

    if (!conditions.empty()) {
        sql += " WHERE ";
        for (size_t i = 0; i < conditions.size(); i++) {
            if (i > 0) sql += " AND ";
            sql += conditions[i];
        }
    }

    sqlite3_stmt* stmt;
    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "filterTopics prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return {};
    }

    for (size_t i = 0; i < params.size(); i++) {
        sqlite3_bind_text(stmt, static_cast<int>(i + 1), params[i].c_str(), -1, SQLITE_TRANSIENT);
    }

    std::vector<Topic> results;
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        results.push_back(rowToTopic(stmt));
    }
    sqlite3_finalize(stmt);

    if (opts.limit > 0 && static_cast<int>(results.size()) > opts.limit) {
        results.resize(opts.limit);
    }

    return results;
}

// ─── Convenience topic filter functions ──────────────────────────────────────

std::vector<Topic> getTopicsByCategory(sqlite3* database, const std::string& category) {
    TopicFilterOptions opts;
    opts.category = category;
    return filterTopics(database, opts);
}

std::vector<Topic> searchTopics(sqlite3* database, const std::string& keyword) {
    TopicFilterOptions opts;
    opts.keyword = keyword;
    return filterTopics(database, opts);
}

// ─── JSON serialization / deserialization ────────────────────────────────────

static crow::json::wvalue topicToJson(const Topic& t) {
    crow::json::wvalue j;
    j["id"]       = t.id;
    j["title"]    = t.topicTitle;
    j["summary"]  = t.topicSummary;
    j["category"] = t.topicCategory;
    return j;
}

static crow::json::wvalue eventToJson(const CalenderEvent& e) {
    crow::json::wvalue j;
    j["id"]       = e.id;
    j["title"]    = e.eventTitle;
    j["summary"]  = e.eventSummary;
    j["category"] = e.eventCategory;
    j["date"]     = e.eventDate;
    j["time"]     = e.eventTime;
    j["archived"] = e.eventArchived;
    return j;
}

static FilterOptions parseFilterOptions(const crow::json::rvalue& j) {
    FilterOptions opts;

    // Parse date filter.
    if (j.has("filter_by_date"))
        opts.filterByDate = j["filter_by_date"].b();
    if (j.has("start_date"))
        opts.startDate = parseDate(std::string(j["start_date"].s()));
    if (j.has("end_date"))
        opts.endDate = parseDate(std::string(j["end_date"].s()));

    // Parse time filter.
    if (j.has("filter_by_time"))
        opts.filterByTime = j["filter_by_time"].b();
    if (j.has("start_time"))
        opts.startTime = parseTime(std::string(j["start_time"].s()));
    if (j.has("end_time"))
        opts.endTime = parseTime(std::string(j["end_time"].s()));

    // Parse categories. These 2 should be combined into one.
    if (j.has("category"))
        opts.category = std::string(j["category"].s());
    if (j.has("categories")) {
        for (size_t i = 0; i < j["categories"].size(); ++i)
            opts.categories.push_back(std::string(j["categories"][i].s()));
    }

    if (j.has("keyword"))
        opts.keyword = std::string(j["keyword"].s());

    if (j.has("archived")) {
        std::string arc = std::string(j["archived"].s());
        if (arc == "all")
            opts.archived = FilterOptions::ArchivedFilter::All;
        else if (arc == "archived_only")
            opts.archived = FilterOptions::ArchivedFilter::ArchivedOnly;
        else if (arc == "active_only")
            opts.archived = FilterOptions::ArchivedFilter::ActiveOnly;
    }

    if (j.has("sort_order")) {
        if (std::string(j["sort_order"].s()) == "desc")
            opts.sortOrder = FilterOptions::SortOrder::DateDesc;
        if (std::string(j["sort_order"].s()) == "asc")
            opts.sortOrder = FilterOptions::SortOrder::DateAsc;
    }

    if (j.has("limit"))
        opts.limit = static_cast<int>(j["limit"].i());

    return opts;
}

static CalenderEvent parseEditEvent(const crow::json::rvalue& j) {
    CalenderEvent event;
    if (j.has("id"))       event.id            = static_cast<int>(j["id"].i());
    if (j.has("title"))    event.eventTitle    = std::string(j["title"].s());
    if (j.has("summary"))  event.eventSummary  = std::string(j["summary"].s());
    if (j.has("category")) event.eventCategory = std::string(j["category"].s());
    if (j.has("date"))     event.eventDate     = std::string(j["date"].s());
    if (j.has("time"))     event.eventTime     = std::string(j["time"].s());
    if (j.has("archived")) event.eventArchived = j["archived"].b();
    return event;
}

static Topic parseEditTopic(const crow::json::rvalue& j) {
    Topic topic;
    if (j.has("id"))       topic.id            = static_cast<int>(j["id"].i());
    if (j.has("title"))    topic.topicTitle    = std::string(j["title"].s());
    if (j.has("summary"))  topic.topicSummary  = std::string(j["summary"].s());
    if (j.has("category")) topic.topicCategory = std::string(j["category"].s());
    return topic;
}

static TopicFilterOptions parseTopicFilterOptions(const crow::json::rvalue& j) {
    TopicFilterOptions opts;
    if (j.has("category")) {
        opts.category = std::string(j["category"].s());
    }
    if (j.has("categories")) {
        for (size_t i = 0; i < j["categories"].size(); ++i)
            opts.categories.push_back(std::string(j["categories"][i].s()));
    }
    if (j.has("keyword"))
        opts.keyword = std::string(j["keyword"].s());
    if (j.has("limit"))
        opts.limit = static_cast<int>(j["limit"].i());
    return opts;
}

// ─── Per-user DB resolution ──────────────────────────────────────────────────

// Returns the sqlite handle for `user_id`, opening and initializing it on
// first use. Always returns a non-null handle (sqlite3_open creates the file
// if it's missing). Thread-safe: the mutex serializes lookup/insertion only;
// the returned handle is then used lock-free by the caller, which is fine
// because sqlite is built in SERIALIZED mode.
static sqlite3* getUserDb(int user_id) {
    std::lock_guard<std::mutex> lock(g_user_dbs_mu);
    auto it = g_user_dbs.find(user_id);
    if (it != g_user_dbs.end()) {
        return it->second;
    }
    std::filesystem::create_directories(userDir(user_id));
    std::string dbPath = userDir(user_id) + "/database.sql";
    sqlite3* db = nullptr;
    openDatabase(const_cast<char*>(dbPath.c_str()), &db);
    createCalender(db);
    createTopics(db);
    g_user_dbs.emplace(user_id, db);
    return db;
}

// Move a legacy single-user layout under <root>/database.sql + <root>/topics/
// to user 1 so existing data keeps working after the multi-user switch.
// Idempotent: if user 1 already has data, the legacy files are left alone
// rather than overwritten (we never want to clobber an existing user 1).
static void migrateLegacyIfPresent() {
    std::string legacyDb = g_root + "/database.sql";
    std::string legacyTopics = g_root + "/topics";
    std::string user1Db = userDir(1) + "/database.sql";
    if (!std::filesystem::exists(legacyDb)) return;
    if (std::filesystem::exists(user1Db)) {
        std::cout << "Legacy DB present at " << legacyDb
                  << " but user 1 already initialized — leaving legacy in place."
                  << std::endl;
        return;
    }
    std::filesystem::create_directories(userDir(1));
    std::error_code ec;
    std::filesystem::rename(legacyDb, user1Db, ec);
    if (ec) {
        std::cout << "Legacy migration: failed to move " << legacyDb << " → "
                  << user1Db << ": " << ec.message() << std::endl;
        return;
    }
    if (std::filesystem::exists(legacyTopics)) {
        std::filesystem::rename(legacyTopics, userTopicsDir(1), ec);
        if (ec) {
            std::cout << "Legacy migration: failed to move topics dir: "
                      << ec.message() << std::endl;
        }
    }
    std::cout << "Migrated legacy database to user 1." << std::endl;
}

// Extract and validate user_id from the request body. Returns true and sets
// `out` on success; on failure populates `err` with an HTTP response.
static bool requireUserId(const crow::json::rvalue& j, int& out, crow::response& err) {
    if (!j.has("user_id")) {
        err = crow::response(400, "missing required field: user_id");
        return false;
    }
    int64_t v = 0;
    try {
        v = j["user_id"].i();
    } catch (...) {
        err = crow::response(400, "user_id must be an integer");
        return false;
    }
    // Positive only: 0 / negative would map to no real account and could
    // collide with the sentinel returned by getEvent/getTopic for "not found".
    if (v <= 0 || v > 2147483647) {
        err = crow::response(400, "user_id out of range");
        return false;
    }
    out = static_cast<int>(v);
    return true;
}

// ─── main ─────────────────────────────────────────────────────────────────────

int main(int argc, char* argv[]) {
    const char* home = std::getenv("HOME");
    g_root = (home ? std::string(home) : std::string("")) +
             "/.local/share/aime-assistant/database";
    if (argc > 1) {
        g_root = argv[1];
    }
    std::filesystem::create_directories(g_root + "/users");
    migrateLegacyIfPresent();

    crow::SimpleApp DatabaseAPI;
    CROW_ROUTE(DatabaseAPI, "/api").methods("POST"_method)([](const crow::request& req){
        auto jsonData = crow::json::load(req.body);
        if (!jsonData) {
            return crow::response(400, "Invalid JSON");
        }

        if (!jsonData.has("tool_name")) {
            return crow::response(400, "Invalid JSON");
        }

        int user_id = 0;
        crow::response authErr;
        if (!requireUserId(jsonData, user_id, authErr)) {
            return authErr;
        }
        sqlite3* database = getUserDb(user_id);

        if (jsonData["tool_name"] == "get_events") {
            FilterOptions opts = parseFilterOptions(jsonData);
            std::vector<CalenderEvent> events = filterEvents(database, opts);

            crow::json::wvalue response;
            response["count"] = static_cast<int>(events.size());
            for (size_t i = 0; i < events.size(); ++i) {
                response["events"][i] = eventToJson(events[i]);
            }

            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "replace_event") {
            if (!jsonData.has("id") || !jsonData.has("title") || !jsonData.has("summary") ||
                !jsonData.has("category") || !jsonData.has("date") ||
                !jsonData.has("archived")) {
                return crow::response(400, "replace_event missing required fields");
            }

            CalenderEvent event = parseEditEvent(jsonData);

            CalenderEvent existing = getEvent(database, event.id);
            if (existing.id == -1) {
                crow::json::wvalue response;
                response["ok"] = false;
                response["error"] = "Event not found. Use create_event to add a new event.";
                response["id"] = event.id;
                return crow::response(404, response);
            }

            updateEvent(database, event);
            if (event.eventArchived)
                archiveEvent(database, event.id);
            else
                unarchiveEvent(database, event.id);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = event.id;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "create_event") {
            if (!jsonData.has("title") || !jsonData.has("summary") ||
                !jsonData.has("category") || !jsonData.has("date") ||
                !jsonData.has("archived")) {
                return crow::response(400, "create_event missing required fields");
            }

            CalenderEvent event = parseEditEvent(jsonData);
            event.id = -1;

            addEvent(database, event);

            if (event.eventArchived)
                archiveEvent(database, event.id);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = event.id;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "get_topics") {
            TopicFilterOptions opts = parseTopicFilterOptions(jsonData);
            std::vector<Topic> topics = filterTopics(database, opts);

            crow::json::wvalue response;
            response["count"] = static_cast<int>(topics.size());
            for (size_t i = 0; i < topics.size(); ++i) {
                response["topics"][i] = topicToJson(topics[i]);
            }

            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "create_topic") {
            if (!jsonData.has("title") || !jsonData.has("summary") ||
                !jsonData.has("category")) {
                return crow::response(400, "create_topic missing required fields");
            }

            Topic topic = parseEditTopic(jsonData);
            topic.id = -1;

            addTopic(database, user_id, topic);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = topic.id;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "replace_topic") {
            if (!jsonData.has("id") || !jsonData.has("summary") || !jsonData.has("category")) {
                return crow::response(400, "replace_topic missing required fields: id, summary, category");
            }

            Topic topic = parseEditTopic(jsonData);

            Topic existing = getTopic(database, topic.id);
            if (existing.id == -1) {
                crow::json::wvalue response;
                response["ok"] = false;
                response["error"] = "Topic not found. Use create_topic to add a new topic.";
                response["id"] = topic.id;
                return crow::response(404, response);
            }

            // Preserve the title — title changes would require renaming the .md file.
            topic.topicTitle = existing.topicTitle;
            updateTopic(database, topic);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = topic.id;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "get_topic_contents") {
            if (!jsonData.has("id")) {
                return crow::response(400, "get_topic_contents missing required field: id");
            }

            int topicID = static_cast<int>(jsonData["id"].i());
            std::string contents = getTopicContents(database, user_id, topicID);

            crow::json::wvalue response;
            response["id"] = topicID;
            response["contents"] = contents;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "replace_topic_contents") {
            if (!jsonData.has("id") || !jsonData.has("contents")) {
                return crow::response(400, "replace_topic_contents missing required fields: id, contents");
            }

            int topicID = static_cast<int>(jsonData["id"].i());
            std::string newContents = std::string(jsonData["contents"].s());

            replaceTopicContents(database, user_id, topicID, newContents);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = topicID;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "edit_topic_contents") {
            if (!jsonData.has("id") || !jsonData.has("patches")) {
                return crow::response(400, "edit_topic_contents missing required fields: id, patches");
            }

            int topicID = static_cast<int>(jsonData["id"].i());

            std::vector<EditPatch> patches;
            auto patchesNode = jsonData["patches"];
            for (size_t i = 0; i < patchesNode.size(); ++i) {
                EditPatch p;
                if (!patchesNode[i].has("find") || !patchesNode[i].has("replace")) {
                    crow::json::wvalue response;
                    response["ok"] = false;
                    response["id"] = topicID;
                    response["error"] = "Patch " + std::to_string(i) + " missing required 'find' or 'replace'.";
                    return crow::response(400, response);
                }
                p.find    = std::string(patchesNode[i]["find"].s());
                p.replace = std::string(patchesNode[i]["replace"].s());
                if (patchesNode[i].has("occurrence")) {
                    p.occurrence = static_cast<int>(patchesNode[i]["occurrence"].i());
                }
                patches.push_back(p);
            }

            EditResult er = editTopicContents(database, user_id, topicID, patches);

            crow::json::wvalue response;
            response["ok"] = er.ok;
            response["id"] = topicID;
            response["applied"] = er.applied;
            if (!er.ok) {
                response["error"] = er.error;
                return crow::response(400, response);
            }
            return crow::response(200, response);
        }

        return crow::response(400, "Invalid Tool Name");
    });

    DatabaseAPI.bindaddr("127.0.0.1").port(8080).multithreaded().run();

    // Close every per-user handle on shutdown. Hold the mutex even though
    // workers should already be stopped at this point — defensive.
    {
        std::lock_guard<std::mutex> lock(g_user_dbs_mu);
        for (auto& kv : g_user_dbs) sqlite3_close(kv.second);
        g_user_dbs.clear();
    }
    return 0;
}
