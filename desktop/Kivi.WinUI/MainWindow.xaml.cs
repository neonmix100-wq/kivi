using System.Diagnostics;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using NAudio.Wave;
using Windows.Media.Core;
using Windows.Storage.Streams;
using Windows.System;
using Windows.UI;

namespace Kivi.WinUI;

public sealed partial class MainWindow : Window
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromMinutes(5) };
    private string _baseUrl = "http://127.0.0.1:8765";
    private Process? _ownedBackend;
    private bool _sending;
    private Border? _pendingBubble;
    private bool _autoSpeak;
    private bool _recording;
    private WaveInEvent? _waveIn;
    private MemoryStream? _micStream;
    private WaveFileWriter? _micWriter;
    private bool _wakeMode;
    private WaveInEvent? _wakeIn;
    private readonly List<byte> _wakePcm = new();
    private readonly object _wakeLock = new();
    private bool _wakeBusy;
    private string _filesPath = ".";
    private string _filesView = ".";
    private string _filesSort = "name";
    private readonly Stack<string> _filesBack = new();
    private TextBox? _filesPathBox;
    private TextBox? _filesSearchBox;
    private StackPanel? _filesListPanel;
    private TextBlock? _filesStatus;
    private readonly List<WinFileEntry> _filesEntries = new();
    private readonly HashSet<string> _filesSelected = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<string> _winTabs = new() { "." };
    private int _winTabIndex;
    private StackPanel? _winTabStrip;

    public MainWindow()
    {
        InitializeComponent();
    }

    private async void Root_Loaded(object sender, RoutedEventArgs e)
    {
        var found = await FindApiAsync();
        if (!found)
        {
            ConnectionText.Text = "Запускаю локальный сервер…";
            StartBackend();
            found = await FindApiAsync(40);
        }
        if (!found)
        {
            ConnectionText.Text = "API недоступен";
            return;
        }
        using var health = await GetJsonAsync("/health");
        if (health is not null && health.RootElement.TryGetProperty("version", out var version))
            VersionText.Text = $"онлайн · v{version.GetString()}";
        ConnectionText.Text = $"API: {_baseUrl.Replace("http://", "")}";
        await LoadHistoryAsync();
    }

    private async Task<bool> FindApiAsync(int attempts = 1)
    {
        for (var attempt = 0; attempt < attempts; attempt++)
        {
            foreach (var port in new[] { 8765, 8766, 8767 })
            {
                var candidate = $"http://127.0.0.1:{port}";
                try
                {
                    using var response = await Http.GetAsync($"{candidate}/health");
                    if (!response.IsSuccessStatusCode) continue;
                    _baseUrl = candidate;
                    return true;
                }
                catch
                {
                    // try next
                }
            }
            if (attempt + 1 < attempts) await Task.Delay(500);
        }
        return false;
    }

    private void StartBackend()
    {
        var root = FindProjectRoot();
        if (root is null) return;
        var pythonw = Path.Combine(root, "runtime", "pythonw.exe");
        if (!File.Exists(pythonw)) pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(pythonw)) return;
        var start = new ProcessStartInfo
        {
            FileName = pythonw,
            Arguments = "-m kivi ui --no-browser --port 8765",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        start.Environment["PYTHONPATH"] = Path.Combine(root, "src") + Path.PathSeparator
            + Path.Combine(root, "runtime", "Lib", "site-packages");
        _ownedBackend = Process.Start(start);
    }

    private static string? FindProjectRoot()
    {
        foreach (var start in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            var current = new DirectoryInfo(start);
            while (current is not null)
            {
                if (File.Exists(Path.Combine(current.FullName, "runtime", "pythonw.exe"))
                    || File.Exists(Path.Combine(current.FullName, ".venv", "Scripts", "pythonw.exe")))
                    return current.FullName;
                current = current.Parent;
            }
        }
        return null;
    }

    private async Task<JsonDocument?> GetJsonAsync(string path)
    {
        try
        {
            using var response = await Http.GetAsync(_baseUrl + path);
            var body = await response.Content.ReadAsStringAsync();
            return response.IsSuccessStatusCode ? JsonDocument.Parse(body) : null;
        }
        catch
        {
            return null;
        }
    }

    private async Task<JsonDocument?> PostJsonAsync(string path, object payload)
    {
        try
        {
            using var response = await Http.PostAsJsonAsync(_baseUrl + path, payload);
            var body = await response.Content.ReadAsStringAsync();
            return response.IsSuccessStatusCode ? JsonDocument.Parse(body) : null;
        }
        catch
        {
            return null;
        }
    }

    private async Task LoadHistoryAsync()
    {
        using var doc = await GetJsonAsync("/history?limit=50");
        if (doc is null || !doc.RootElement.TryGetProperty("messages", out var messages)) return;
        MessagesPanel.Children.Clear();
        MessagesPanel.Children.Add(DateLabel("ИСТОРИЯ"));
        foreach (var message in messages.EnumerateArray())
        {
            var role = message.TryGetProperty("role", out var r) ? r.GetString() : "";
            var content = message.TryGetProperty("content", out var c) ? c.GetString() : "";
            if (!string.IsNullOrWhiteSpace(content)) AddBubble(content!, role == "user");
        }
    }

    private void NewChat_Click(object sender, RoutedEventArgs e)
    {
        MessagesPanel.Children.Clear();
        MessagesPanel.Children.Add(DateLabel("НОВЫЙ ЧАТ"));
        AddBubble("Привет! Я Kivi. Чем помочь?", false);
        ComposerInput.Focus(FocusState.Programmatic);
    }

    private void Nav_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button button) return;
        var tag = button.Tag?.ToString() ?? "chat";
        var titles = new Dictionary<string, string>
        {
            ["chat"] = "Чат", ["voices"] = "Голоса", ["media"] = "Медиа",
            ["files"] = "Файлы", ["crm"] = "CRM", ["settings"] = "Настройки",
        };
        PageTitle.Text = titles.GetValueOrDefault(tag, "Kivi");
        ChatView.Visibility = tag == "chat" ? Visibility.Visible : Visibility.Collapsed;
        PageView.Visibility = tag == "chat" ? Visibility.Collapsed : Visibility.Visible;
        if (tag != "chat") _ = BuildPageAsync(tag);
    }

    private async Task BuildPageAsync(string tag)
    {
        PagePanel.Children.Clear();
        switch (tag)
        {
            case "voices": await BuildVoicesAsync(); break;
            case "settings": await BuildSettingsAsync(); break;
            case "media": await BuildMediaAsync(); break;
            case "files": await BuildFilesAsync(); break;
            case "crm": await BuildCrmAsync(); break;
        }
    }

    private void BuildPlaceholder(string title, string text)
    {
        PagePanel.Children.Add(Heading(title));
        PagePanel.Children.Add(Sub(text));
    }

    private TextBlock Heading(string text) => new()
    {
        Text = text,
        Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
        FontSize = 24,
        FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
    };

    private TextBlock Sub(string text) => new()
    {
        Text = text,
        Foreground = new SolidColorBrush(Color.FromArgb(255, 143, 184, 176)),
        FontSize = 13,
        TextWrapping = TextWrapping.Wrap,
        Margin = new Thickness(0, 8, 0, 14),
    };

    private Button PageButton(string text, RoutedEventHandler click)
    {
        var button = new Button
        {
            Content = text,
            Background = new SolidColorBrush(Color.FromArgb(255, 11, 58, 52)),
            Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
            BorderThickness = new Thickness(0),
            Padding = new Thickness(12, 8, 12, 8),
            Margin = new Thickness(0, 0, 8, 8),
            HorizontalAlignment = HorizontalAlignment.Left,
        };
        button.Click += click;
        return button;
    }

    private async Task BuildVoicesAsync()
    {
        PagePanel.Children.Add(Heading("Голоса"));
        PagePanel.Children.Add(Sub("Выбери голосовой движок. Активный голос хранится на сервере."));
        using var doc = await GetJsonAsync("/voices");
        if (doc is null) { PagePanel.Children.Add(Sub("Не удалось загрузить голоса.")); return; }
        if (!doc.RootElement.TryGetProperty("voices", out var voices)) return;
        foreach (var voice in voices.EnumerateArray())
        {
            var id = voice.TryGetProperty("id", out var i) ? i.GetString() ?? "" : "";
            var name = voice.TryGetProperty("name", out var n) ? n.GetString() ?? id : id;
            var engine = voice.TryGetProperty("engine", out var e) ? e.GetString() ?? "" : "";
            var active = voice.TryGetProperty("active", out var a) && a.GetBoolean();
            var available = !voice.TryGetProperty("available", out var av) || av.GetBoolean();
            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, Padding = new Thickness(12) };
            row.Children.Add(new TextBlock
            {
                Text = $"{name} · {engine}",
                Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
                FontSize = 15,
                VerticalAlignment = VerticalAlignment.Center,
                Width = 360,
            });
            var select = PageButton(active ? "Активен" : available ? "Выбрать" : "Недоступен", async (_, _) =>
            {
                if (!available) return;
                using var result = await PostJsonAsync("/voices/active", new { id });
                await BuildPageAsync("voices");
            });
            select.IsEnabled = available && !active;
            row.Children.Add(select);
            PagePanel.Children.Add(new Border
            {
                Background = new SolidColorBrush(Color.FromArgb(255, 8, 47, 43)),
                BorderBrush = new SolidColorBrush(Color.FromArgb(255, 22, 68, 61)),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(12),
                Margin = new Thickness(0, 0, 0, 10),
                Child = row,
            });
        }
    }

    private async Task BuildSettingsAsync()
    {
        PagePanel.Children.Add(Heading("Настройки"));
        PagePanel.Children.Add(Sub("Провайдер и параметры синтеза речи."));
        using var doc = await GetJsonAsync("/settings");
        if (doc is null) { PagePanel.Children.Add(Sub("Не удалось загрузить настройки.")); return; }
        var providers = new[] { "qwen", "silero", "piper", "fish" };
        var combo = new ComboBox { Width = 260, HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 0, 0, 8) };
        foreach (var provider in providers) combo.Items.Add(provider);
        var current = doc.RootElement.TryGetProperty("voice_provider", out var p) ? p.GetString() : "qwen";
        combo.SelectedItem = providers.Contains(current) ? current : "qwen";
        PagePanel.Children.Add(combo);
        var model = SettingsBox(doc, "model");
        var speaker = SettingsBox(doc, "speaker");
        var rate = SettingsBox(doc, "sample_rate");
        PagePanel.Children.Add(SettingsLabel("model")); PagePanel.Children.Add(model);
        PagePanel.Children.Add(SettingsLabel("speaker")); PagePanel.Children.Add(speaker);
        PagePanel.Children.Add(SettingsLabel("sample_rate")); PagePanel.Children.Add(rate);
        PagePanel.Children.Add(PageButton("Сохранить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/settings", new
            {
                voice_provider = combo.SelectedItem?.ToString(),
                model = model.Text,
                speaker = speaker.Text,
                sample_rate = int.TryParse(rate.Text, out var value) ? value : 48000,
            });
            await BuildPageAsync("settings");
        }));
    }

    private TextBlock SettingsLabel(string text) => new()
    {
        Text = text,
        Foreground = new SolidColorBrush(Color.FromArgb(255, 143, 184, 176)),
        FontSize = 11,
    };

    private TextBox SettingsBox(JsonDocument doc, string key)
    {
        var value = doc.RootElement.TryGetProperty(key, out var property) ? property.ToString() : "";
        return new TextBox
        {
            Text = value,
            Width = 320,
            HorizontalAlignment = HorizontalAlignment.Left,
            Background = new SolidColorBrush(Color.FromArgb(255, 8, 47, 43)),
            Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
            BorderBrush = new SolidColorBrush(Color.FromArgb(255, 22, 68, 61)),
            Margin = new Thickness(0, 0, 0, 8),
        };
    }

    private readonly List<(string Kind, string Value)> _winMedia = new();
    private StackPanel? _winGalleryPanel;

    private async Task BuildMediaAsync()
    {
        PagePanel.Children.Add(Heading("Медиа"));
        PagePanel.Children.Add(Sub("Генерация изображений и видео через локальный API."));
        var prompt = new TextBox
        {
            PlaceholderText = "Опиши изображение",
            Width = 560,
            HorizontalAlignment = HorizontalAlignment.Left,
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 8),
        };
        PagePanel.Children.Add(prompt);
        var output = new StackPanel();
        PagePanel.Children.Add(PageButton("Сгенерировать изображение", async (_, _) =>
        {
            output.Children.Clear();
            using var result = await PostJsonAsync("/media/image", new
            {
                prompt = prompt.Text,
                width = 512,
                height = 512,
                model = "flux",
            });
            if (result is null) { output.Children.Add(Sub("Генерация недоступна.")); return; }
            var image = result.RootElement.TryGetProperty("image", out var value) ? value.GetString() : null;
            if (image is null || !image.StartsWith("data:image/", StringComparison.Ordinal)) return;
            var comma = image.IndexOf(',');
            if (comma <= 0) return;
            var bytes = Convert.FromBase64String(image[(comma + 1)..]);
            output.Children.Add(await BuildWinImageAsync(bytes));
            _winMedia.Insert(0, ("image", Convert.ToBase64String(bytes)));
            TrimWinMedia();
            RenderWinGallery();
        }));
        PagePanel.Children.Add(PageButton("Сгенерировать видео 4 с", async (_, _) =>
        {
            output.Children.Clear();
            using var result = await PostJsonAsync("/media/video", new
            {
                prompt = prompt.Text,
                duration = 4,
                model = "local",
            });
            if (result is null) { output.Children.Add(Sub("Видео недоступно.")); return; }
            var video = result.RootElement.TryGetProperty("video", out var value) ? value.GetString() : null;
            if (video is null || !video.StartsWith("data:video/", StringComparison.Ordinal)) return;
            var comma = video.IndexOf(',');
            if (comma <= 0) return;
            var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"kivi-{Guid.NewGuid():N}.mp4");
            await File.WriteAllBytesAsync(path, Convert.FromBase64String(video[(comma + 1)..]));
            output.Children.Add(BuildWinVideo(path));
            _winMedia.Insert(0, ("video", path));
            TrimWinMedia();
            RenderWinGallery();
        }));
        PagePanel.Children.Add(output);
        PagePanel.Children.Add(new TextBlock
        {
            Text = "Галерея",
            Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
            FontSize = 18,
            FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
            Margin = new Thickness(0, 20, 0, 8),
        });
        _winGalleryPanel = new StackPanel { Spacing = 8 };
        PagePanel.Children.Add(_winGalleryPanel);
        RenderWinGallery();
        await Task.CompletedTask;
    }

    private void TrimWinMedia()
    {
        while (_winMedia.Count > 12) _winMedia.RemoveAt(_winMedia.Count - 1);
    }

    private void RenderWinGallery()
    {
        if (_winGalleryPanel is null) return;
        _winGalleryPanel.Children.Clear();
        if (_winMedia.Count == 0)
        {
            _winGalleryPanel.Children.Add(Sub("Здесь появятся сгенерированные изображения и видео."));
            return;
        }
        foreach (var (kind, value) in _winMedia)
        {
            if (kind == "image")
            {
                var bitmap = new BitmapImage();
                using var stream = new InMemoryRandomAccessStream();
                using var writer = new DataWriter(stream);
                writer.WriteBytes(Convert.FromBase64String(value));
                writer.StoreAsync().AsTask().GetAwaiter().GetResult();
                writer.FlushAsync().AsTask().GetAwaiter().GetResult();
                writer.DetachStream();
                stream.Seek(0);
                bitmap.SetSourceAsync(stream).AsTask().GetAwaiter().GetResult();
                _winGalleryPanel.Children.Add(new Image { Source = bitmap, MaxWidth = 320, MaxHeight = 320, HorizontalAlignment = HorizontalAlignment.Left });
            }
            else if (File.Exists(value))
            {
                _winGalleryPanel.Children.Add(BuildWinVideo(value));
            }
        }
    }

    private static MediaPlayerElement BuildWinVideo(string path) => new()
    {
        Source = MediaSource.CreateFromUri(new Uri(path)),
        Width = 480,
        Height = 270,
        AreTransportControlsEnabled = true,
        HorizontalAlignment = HorizontalAlignment.Left,
    };

    private static async Task<Image> BuildWinImageAsync(byte[] bytes)
    {
        var bitmap = new BitmapImage();
        using var stream = new InMemoryRandomAccessStream();
        using var writer = new DataWriter(stream);
        writer.WriteBytes(bytes);
        await writer.StoreAsync();
        await writer.FlushAsync();
        writer.DetachStream();
        stream.Seek(0);
        await bitmap.SetSourceAsync(stream);
        return new Image { Source = bitmap, MaxWidth = 420, MaxHeight = 420, HorizontalAlignment = HorizontalAlignment.Left };
    }

    private async Task BuildFilesAsync()
    {
        PagePanel.Children.Add(Heading("Файлы"));
        PagePanel.Children.Add(Sub("Навигация, поиск, сортировка, preview и операции."));
        _winTabStrip = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 4, Margin = new Thickness(0, 0, 0, 8) };
        PagePanel.Children.Add(_winTabStrip);
        RenderWinTabs();

        var toolbar = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6, Margin = new Thickness(0, 0, 0, 8) };
        toolbar.Children.Add(PageButton("Компьютер", async (_, _) => await LoadFilesAsync("shell://")));
        toolbar.Children.Add(PageButton("Назад", async (_, _) =>
        {
            if (_filesBack.Count > 0) await LoadFilesAsync(_filesBack.Pop(), false);
        }));
        toolbar.Children.Add(PageButton("Вверх", async (_, _) =>
        {
            var parent = WinParent(_filesView);
            if (parent is not null) await LoadFilesAsync(parent);
        }));
        toolbar.Children.Add(PageButton("Новая папка", async (_, _) => await WinMkdirAsync()));
        toolbar.Children.Add(PageButton("Отменить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/fs/undo", new { });
            await LoadFilesAsync(_filesPath, false);
        }));
        PagePanel.Children.Add(toolbar);

        _filesPathBox = new TextBox
        {
            Text = _filesPath,
            Width = 620,
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 0, 8, 8),
        };
        var pathRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        pathRow.Children.Add(_filesPathBox);
        pathRow.Children.Add(PageButton("Перейти", async (_, _) => await LoadFilesAsync(_filesPathBox.Text)));
        PagePanel.Children.Add(pathRow);

        _filesSearchBox = new TextBox
        {
            PlaceholderText = "Поиск по имени",
            Width = 420,
            HorizontalAlignment = HorizontalAlignment.Left,
        };
        var searchRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 0, 0, 8) };
        searchRow.Children.Add(_filesSearchBox);
        searchRow.Children.Add(PageButton("Найти", async (_, _) => await SearchFilesAsync()));
        searchRow.Children.Add(PageButton("Очистить", async (_, _) => await LoadFilesAsync(_filesPath, false)));
        PagePanel.Children.Add(searchRow);

        var sort = new ComboBox { Width = 190, HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 0, 0, 8) };
        foreach (var option in new[] { "По имени", "По размеру", "По типу", "По дате" }) sort.Items.Add(option);
        sort.SelectedIndex = _filesSort switch { "size" => 1, "type" => 2, "modified" => 3, _ => 0 };
        sort.SelectionChanged += (_, _) =>
        {
            _filesSort = sort.SelectedIndex switch { 1 => "size", 2 => "type", 3 => "modified", _ => "name" };
            RenderFiles();
        };
        PagePanel.Children.Add(sort);

        _filesStatus = Sub("Загружаю…");
        PagePanel.Children.Add(_filesStatus);
        _filesListPanel = new StackPanel { Spacing = 4 };
        PagePanel.Children.Add(_filesListPanel);
        await LoadFilesAsync(_filesPath);
    }

    private void RenderWinTabs()
    {
        if (_winTabStrip is null) return;
        _winTabStrip.Children.Clear();
        for (var index = 0; index < _winTabs.Count; index++)
        {
            var tabIndex = index;
            var path = _winTabs[index];
            var label = path == "." ? "Рабочая" : path == "shell://" ? "Компьютер" : System.IO.Path.GetFileName(path.TrimEnd('/', '\\'));
            if (string.IsNullOrWhiteSpace(label)) label = path;
            var button = PageButton(label, async (_, _) =>
            {
                _winTabIndex = tabIndex;
                await LoadFilesAsync(_winTabs[tabIndex], false);
            });
            button.Background = tabIndex == _winTabIndex
                ? new SolidColorBrush(Color.FromArgb(255, 11, 58, 52))
                : new SolidColorBrush(Color.FromArgb(255, 8, 47, 43));
            _winTabStrip.Children.Add(button);
        }
        _winTabStrip.Children.Add(PageButton("+", async (_, _) =>
        {
            _winTabs.Add(_filesView);
            _winTabIndex = _winTabs.Count - 1;
            RenderWinTabs();
            await LoadFilesAsync(_filesView, false);
        }));
    }

    private async Task LoadFilesAsync(string path, bool record = true)
    {
        if (_filesListPanel is null || _filesStatus is null) return;
        if (string.IsNullOrWhiteSpace(path)) path = ".";
        if (record && !string.Equals(_filesPath, path, StringComparison.OrdinalIgnoreCase))
            _filesBack.Push(_filesPath);
        _filesPath = path;
        if (_winTabIndex >= 0 && _winTabIndex < _winTabs.Count) _winTabs[_winTabIndex] = path;
        RenderWinTabs();
        if (_filesPathBox is not null) _filesPathBox.Text = path;
        _filesStatus.Text = "Загружаю…";
        _filesListPanel.Children.Clear();
        using var doc = await GetJsonAsync($"/fs/list?path={Uri.EscapeDataString(path)}");
        if (doc is null)
        {
            _filesStatus.Text = "Не удалось открыть папку.";
            return;
        }
        _filesView = doc.RootElement.TryGetProperty("path", out var resolved) ? resolved.GetString() ?? path : path;
        _filesEntries.Clear();
        if (doc.RootElement.TryGetProperty("entries", out var entries))
        {
            foreach (var entry in entries.EnumerateArray())
            {
                var name = entry.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                var type = entry.TryGetProperty("type", out var t) ? t.GetString() ?? "file" : "file";
                var entryPath = entry.TryGetProperty("path", out var ep) ? ep.GetString() : null;
                entryPath ??= WinJoin(_filesView, name);
                var size = entry.TryGetProperty("size", out var s) && s.TryGetInt64(out var bytes) ? bytes : 0;
                var mtime = entry.TryGetProperty("mtime", out var m) && m.TryGetDouble(out var stamp) ? stamp : 0;
                _filesEntries.Add(new WinFileEntry(name, type, entryPath, size, mtime));
            }
        }
        _filesStatus.Text = $"{_filesEntries.Count} объектов · {_filesView}";
        RenderFiles();
    }

    private void RenderFiles()
    {
        if (_filesListPanel is null) return;
        _filesListPanel.Children.Clear();
        IEnumerable<WinFileEntry> ordered = _filesSort switch
        {
            "size" => _filesEntries.OrderByDescending(item => item.Size),
            "type" => _filesEntries.OrderBy(item => item.Type).ThenBy(item => item.Name),
            "modified" => _filesEntries.OrderByDescending(item => item.Mtime),
            _ => _filesEntries.OrderBy(item => item.Name, StringComparer.CurrentCultureIgnoreCase),
        };
        foreach (var entry in ordered)
            _filesListPanel.Children.Add(WinFileRow(entry));
    }

    private Button WinFileRow(WinFileEntry entry)
    {
        var content = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        var iconHost = new Border
        {
            Width = 30,
            Height = 30,
            CornerRadius = new CornerRadius(7),
            Background = new SolidColorBrush(Color.FromArgb(255, 10, 55, 49)),
        };
        iconHost.Child = new TextBlock
        {
            Text = entry.Type == "dir" ? "▸" : "·",
            Foreground = new SolidColorBrush(Color.FromArgb(255, 94, 242, 214)),
            FontSize = 18,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        content.Children.Add(iconHost);
        if (entry.Type != "dir") _ = LoadWinThumbAsync(iconHost, entry.Path);
        content.Children.Add(new TextBlock
        {
            Text = entry.Name,
            Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
            FontSize = 14,
            VerticalAlignment = VerticalAlignment.Center,
        });
        var button = new Button
        {
            Content = content,
            HorizontalContentAlignment = HorizontalAlignment.Left,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            Padding = new Thickness(10, 7, 10, 7),
            Background = _filesSelected.Contains(entry.Path)
                ? new SolidColorBrush(Color.FromArgb(255, 11, 58, 52))
                : new SolidColorBrush(Color.FromArgb(255, 8, 47, 43)),
            BorderBrush = new SolidColorBrush(Color.FromArgb(255, 22, 68, 61)),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
        };
        button.Click += async (_, _) =>
        {
            if ((Microsoft.UI.Input.InputKeyboardSource.GetKeyStateForCurrentThread(VirtualKey.Control)
                    & Windows.UI.Core.CoreVirtualKeyStates.Down) != 0)
            {
                if (!_filesSelected.Add(entry.Path)) _filesSelected.Remove(entry.Path);
                RenderFiles();
                return;
            }
            if (entry.Type == "dir") await LoadFilesAsync(entry.Path);
            else await ShowFilePreviewAsync(entry);
        };
        var flyout = new MenuFlyout();
        var rename = new MenuFlyoutItem { Text = "Переименовать" };
        rename.Click += async (_, _) => await WinRenameAsync(entry);
        var copy = new MenuFlyoutItem { Text = "Копировать в…" };
        copy.Click += async (_, _) => await WinTransferAsync(entry, "copy");
        var move = new MenuFlyoutItem { Text = "Переместить в…" };
        move.Click += async (_, _) => await WinTransferAsync(entry, "move");
        var trash = new MenuFlyoutItem { Text = "В корзину" };
        trash.Click += async (_, _) => await WinTrashAsync(entry);
        var explorer = new MenuFlyoutItem { Text = "Открыть в Проводнике" };
        explorer.Click += async (_, _) => await PostJsonAsync("/fs/reveal", new { path = entry.Path });
        flyout.Items.Add(rename);
        flyout.Items.Add(copy);
        flyout.Items.Add(move);
        flyout.Items.Add(trash);
        flyout.Items.Add(explorer);
        button.ContextFlyout = flyout;
        button.AllowDrop = true;
        button.DragOver += (_, args) =>
        {
            args.AcceptedOperation = Windows.ApplicationModel.DataTransfer.DataPackageOperation.Copy;
        };
        button.Drop += async (_, args) =>
        {
            if (!args.DataView.Contains(Windows.ApplicationModel.DataTransfer.StandardDataFormats.StorageItems)) return;
            var items = await args.DataView.GetStorageItemsAsync();
            foreach (var item in items.OfType<Windows.Storage.StorageFile>())
            {
                using var result = await PostJsonAsync("/fs/copy", new
                {
                    source = item.Path,
                    destination = WinJoin(_filesView, item.Name),
                });
            }
            args.Handled = true;
            await LoadFilesAsync(_filesPath, false);
        };
        return button;
    }

    private async Task LoadWinThumbAsync(Border host, string path)
    {
        try
        {
            var bytes = await Http.GetByteArrayAsync(
                $"{_baseUrl}/fs/thumb?path={Uri.EscapeDataString(path)}");
            var bitmap = new BitmapImage();
            using var stream = new InMemoryRandomAccessStream();
            using var writer = new DataWriter(stream);
            writer.WriteBytes(bytes);
            await writer.StoreAsync();
            await writer.FlushAsync();
            writer.DetachStream();
            stream.Seek(0);
            await bitmap.SetSourceAsync(stream);
            host.Child = new Image
            {
                Source = bitmap,
                Stretch = Stretch.UniformToFill,
                Width = 30,
                Height = 30,
            };
        }
        catch
        {
            // keep the glyph when no thumbnail exists
        }
    }

    private async Task WinTransferAsync(WinFileEntry entry, string mode)
    {
        var destination = await PromptAsync(mode == "copy" ? "Копировать" : "Переместить", "Куда (полный путь)", _filesView);
        if (string.IsNullOrWhiteSpace(destination)) return;
        await PostJsonAsync(mode == "copy" ? "/fs/copy" : "/fs/move", new
        {
            source = entry.Path,
            destination = WinJoin(destination.Trim(), entry.Name),
        });
        await LoadFilesAsync(_filesPath, false);
    }

    private async Task SearchFilesAsync()
    {
        if (_filesSearchBox is null || _filesListPanel is null || _filesStatus is null) return;
        var query = _filesSearchBox.Text.Trim();
        if (query.Length == 0) { await LoadFilesAsync(_filesPath, false); return; }
        _filesStatus.Text = "Ищу…";
        _filesListPanel.Children.Clear();
        using var doc = await GetJsonAsync(
            $"/fs/search?q={Uri.EscapeDataString(query)}&path={Uri.EscapeDataString(_filesView)}");
        if (doc is null) { _filesStatus.Text = "Поиск не удался."; return; }
        _filesEntries.Clear();
        if (doc.RootElement.TryGetProperty("matches", out var matches))
        {
            foreach (var entry in matches.EnumerateArray())
            {
                var name = entry.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                var type = entry.TryGetProperty("type", out var t) ? t.GetString() ?? "file" : "file";
                var path = entry.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
                if (path.Length == 0) continue;
                _filesEntries.Add(new WinFileEntry(name, type, path, 0, 0));
            }
        }
        _filesStatus.Text = $"{_filesEntries.Count} результатов поиска.";
        RenderFiles();
    }

    private async Task ShowFilePreviewAsync(WinFileEntry entry)
    {
        using var doc = await GetJsonAsync($"/fs/read?path={Uri.EscapeDataString(entry.Path)}");
        if (doc is null) return;
        var content = doc.RootElement.TryGetProperty("content", out var value) ? value.GetString() : "";
        var box = new TextBox
        {
            Text = content,
            IsReadOnly = true,
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            MaxHeight = 260,
            Margin = new Thickness(0, 10, 0, 0),
        };
        PagePanel.Children.Add(box);
    }

    private async Task WinMkdirAsync()
    {
        var name = await PromptAsync("Новая папка", "Имя папки");
        if (string.IsNullOrWhiteSpace(name)) return;
        await PostJsonAsync("/fs/mkdir", new { path = WinJoin(_filesView, name.Trim()) });
        await LoadFilesAsync(_filesPath, false);
    }

    private async Task WinRenameAsync(WinFileEntry entry)
    {
        var name = await PromptAsync("Переименовать", "Новое имя", entry.Name);
        if (string.IsNullOrWhiteSpace(name) || name == entry.Name) return;
        var parent = WinParent(entry.Path) ?? _filesView;
        await PostJsonAsync("/fs/rename", new { source = entry.Path, destination = WinJoin(parent, name.Trim()) });
        await LoadFilesAsync(_filesPath, false);
    }

    private async Task WinTrashAsync(WinFileEntry entry)
    {
        var dialog = new ContentDialog
        {
            Title = "Kivi",
            Content = $"Переместить «{entry.Name}» в Корзину?",
            PrimaryButtonText = "В корзину",
            CloseButtonText = "Отмена",
            XamlRoot = Content.XamlRoot,
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        await PostJsonAsync("/fs/trash", new { path = entry.Path, confirm = entry.Name });
        await LoadFilesAsync(_filesPath, false);
    }

    private async Task<string> PromptAsync(string title, string label, string initial = "")
    {
        var box = new TextBox { Text = initial, PlaceholderText = label };
        var dialog = new ContentDialog
        {
            Title = title,
            Content = box,
            PrimaryButtonText = "OK",
            CloseButtonText = "Отмена",
            XamlRoot = Content.XamlRoot,
        };
        return await dialog.ShowAsync() == ContentDialogResult.Primary ? box.Text : "";
    }

    private static string? WinParent(string path)
    {
        if (path == "shell://" || path == ".") return null;
        var trimmed = path.TrimEnd('/', '\\');
        if (trimmed.Length == 2 && trimmed[1] == ':') return "shell://";
        var index = Math.Max(trimmed.LastIndexOf('/'), trimmed.LastIndexOf('\\'));
        if (index <= 0) return ".";
        var parent = trimmed[..index];
        return parent.Length == 2 && parent[1] == ':' ? parent + "/" : parent;
    }

    private static string WinJoin(string directory, string name)
    {
        if (directory == "shell://") return name;
        return directory.EndsWith('/') || directory.EndsWith('\\') ? directory + name : directory + "/" + name;
    }

    private sealed record WinFileEntry(string Name, string Type, string Path, long Size, double Mtime);

    private async Task BuildCrmAsync()
    {
        PagePanel.Children.Add(Heading("CRM"));
        PagePanel.Children.Add(Sub("Статус AltCRM и карточка заказа."));
        using var doc = await GetJsonAsync("/crm/status");
        var connected = doc?.RootElement.TryGetProperty("connected", out var c) == true && c.GetBoolean();
        PagePanel.Children.Add(Sub(connected ? "Расширение подключено." : "Расширение не подключено."));
        var readButton = PageButton("Прочитать заказ", async (_, _) =>
        {
            using var result = await GetJsonAsync("/crm/read");
            if (result is not null) PagePanel.Children.Add(Sub(result.RootElement.ToString()));
        });
        PagePanel.Children.Add(readButton);
        var comment = new TextBox { PlaceholderText = "Комментарий", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        var deadline = new TextBox { PlaceholderText = "Согласованные сроки ремонта", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        PagePanel.Children.Add(comment);
        PagePanel.Children.Add(deadline);
        PagePanel.Children.Add(PageButton("Заполнить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/crm/fill", new
            {
                comment = comment.Text,
                checklist = new Dictionary<string, string> { ["deadline"] = deadline.Text },
            });
            PagePanel.Children.Add(Sub(result is null ? "Не удалось заполнить." : "Заполнено, не сохранено."));
        }));
        var work = new TextBox { PlaceholderText = "Работа", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        var price = new TextBox { PlaceholderText = "Цена, ₽", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        var warranty = new TextBox { PlaceholderText = "Гарантия, дней", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        PagePanel.Children.Add(work);
        PagePanel.Children.Add(price);
        PagePanel.Children.Add(warranty);
        PagePanel.Children.Add(PageButton("Добавить строку акта", async (_, _) =>
        {
            using var result = await PostJsonAsync("/crm/act", new
            {
                rows = new[]
                {
                    new
                    {
                        name = work.Text,
                        price = int.TryParse(price.Text, out var p) ? p : 0,
                        warranty = int.TryParse(warranty.Text, out var w) ? w : 0,
                    },
                },
            });
            PagePanel.Children.Add(Sub(result is null ? "Не удалось добавить." : "Строка добавлена, не сохранено."));
        }));
        var task = new TextBox { PlaceholderText = "Текст задачи", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        var when = new TextBox { PlaceholderText = "Срок, например завтра", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        PagePanel.Children.Add(task);
        PagePanel.Children.Add(when);
        PagePanel.Children.Add(PageButton("Заполнить задачу", async (_, _) =>
        {
            using var result = await PostJsonAsync("/crm/task", new { comment = task.Text, when_text = when.Text });
            PagePanel.Children.Add(Sub(result is null ? "Не удалось заполнить." : "Задача заполнена, не создана."));
        }));
    }

    private static TextBlock DateLabel(string text) => new()
    {
        Text = text,
        Foreground = new SolidColorBrush(Color.FromArgb(255, 143, 184, 176)),
        FontSize = 11,
        HorizontalAlignment = HorizontalAlignment.Center,
        Margin = new Thickness(0, 4, 0, 22),
    };

    private void ComposerInput_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != VirtualKey.Enter) return;
        var shift = Microsoft.UI.Input.InputKeyboardSource
            .GetKeyStateForCurrentThread(VirtualKey.Shift)
            .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
        if (shift) return;
        e.Handled = true;
        _ = SendMessageAsync();
    }

    private void Send_Click(object sender, RoutedEventArgs e) => _ = SendMessageAsync();

    private async void Mic_Click(object sender, RoutedEventArgs e)
    {
        if (_recording)
        {
            await StopRecordingAsync();
            return;
        }
        try
        {
            _micStream = new MemoryStream();
            _micWriter = new WaveFileWriter(_micStream, new WaveFormat(16000, 16, 1));
            _waveIn = new WaveInEvent { WaveFormat = new WaveFormat(16000, 16, 1), BufferMilliseconds = 100 };
            _waveIn.DataAvailable += (_, args) =>
            {
                _micWriter?.Write(args.Buffer, 0, args.BytesRecorded);
                _micWriter?.Flush();
            };
            _waveIn.StartRecording();
            _recording = true;
        }
        catch (Exception ex)
        {
            AddBubble($"Микрофон недоступен: {ex.Message}", false);
        }
    }

    private async Task StopRecordingAsync()
    {
        if (!_recording) return;
        _recording = false;
        _waveIn?.StopRecording();
        _waveIn?.Dispose();
        _waveIn = null;
        _micWriter?.Dispose();
        _micWriter = null;
        if (_micStream is null) return;
        var audio = _micStream.ToArray();
        _micStream.Dispose();
        _micStream = null;
        try
        {
            using var content = new ByteArrayContent(audio);
            content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("audio/wav");
            using var response = await Http.PostAsync($"{_baseUrl}/transcribe", content);
            if (!response.IsSuccessStatusCode) return;
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            var text = doc.RootElement.TryGetProperty("text", out var value) ? value.GetString() : "";
            if (!string.IsNullOrWhiteSpace(text)) ComposerInput.Text = text;
        }
        catch (Exception ex)
        {
            AddBubble($"Не удалось распознать речь: {ex.Message}", false);
        }
    }

    private async Task SpeakAsync(string text)
    {
        try
        {
            using var response = await Http.PostAsJsonAsync($"{_baseUrl}/tts", new { text });
            if (!response.IsSuccessStatusCode) return;
            var audio = await response.Content.ReadAsByteArrayAsync();
            await Task.Run(async () =>
            {
                using var stream = new MemoryStream(audio);
                using var reader = new WaveFileReader(stream);
                using var output = new WaveOutEvent();
                output.Init(reader);
                output.Play();
                while (output.PlaybackState == PlaybackState.Playing) await Task.Delay(100);
            });
        }
        catch
        {
            // best effort
        }
    }
    private void Speak_Click(object sender, RoutedEventArgs e) => _autoSpeak = !_autoSpeak;
    private void Wake_Click(object sender, RoutedEventArgs e)
    {
        if (_wakeMode)
        {
            StopWake();
            return;
        }
        try
        {
            _wakeMode = true;
            lock (_wakeLock) _wakePcm.Clear();
            _wakeIn = new WaveInEvent { WaveFormat = new WaveFormat(16000, 16, 1), BufferMilliseconds = 200 };
            _wakeIn.DataAvailable += (_, args) =>
            {
                lock (_wakeLock)
                {
                    _wakePcm.AddRange(args.Buffer.Take(args.BytesRecorded));
                    if (_wakePcm.Count > 16000 * 2 * 4)
                        _wakePcm.RemoveRange(0, _wakePcm.Count - 16000 * 2 * 4);
                }
            };
            _wakeIn.StartRecording();
            ConnectionText.Text = "Слушаю «Киви»…";
            _ = WakeLoopAsync();
        }
        catch (Exception ex)
        {
            _wakeMode = false;
            AddBubble($"Wake недоступен: {ex.Message}", false);
        }
    }

    private async Task WakeLoopAsync()
    {
        while (_wakeMode)
        {
            await Task.Delay(1200);
            if (!_wakeMode || _wakeBusy) continue;
            byte[] pcm;
            lock (_wakeLock) pcm = _wakePcm.ToArray();
            if (pcm.Length < 32000) continue;
            _wakeBusy = true;
            try
            {
                var wav = PcmToWav(pcm);
                using var content = new ByteArrayContent(wav);
                content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("audio/wav");
                using var response = await Http.PostAsync($"{_baseUrl}/wake", content);
                if (!response.IsSuccessStatusCode) continue;
                using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                if (!(doc.RootElement.TryGetProperty("detected", out var value) && value.GetBoolean())) continue;
                StopWake();
                using var audio = new ByteArrayContent(wav);
                audio.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("audio/wav");
                using var stt = await Http.PostAsync($"{_baseUrl}/transcribe", audio);
                if (!stt.IsSuccessStatusCode) return;
                using var sttDoc = JsonDocument.Parse(await stt.Content.ReadAsStringAsync());
                var text = sttDoc.RootElement.TryGetProperty("text", out var t) ? t.GetString() : "";
                if (!string.IsNullOrWhiteSpace(text))
                {
                    ComposerInput.Text = text;
                    await SendMessageAsync();
                }
                return;
            }
            catch
            {
                // keep listening after a transient probe failure
            }
            finally
            {
                _wakeBusy = false;
            }
        }
    }

    private void StopWake()
    {
        _wakeMode = false;
        _wakeIn?.StopRecording();
        _wakeIn?.Dispose();
        _wakeIn = null;
        ConnectionText.Text = $"API: {_baseUrl.Replace("http://", "")}";
    }

    private static byte[] PcmToWav(byte[] pcm)
    {
        using var stream = new MemoryStream();
        using (var writer = new WaveFileWriter(stream, new WaveFormat(16000, 16, 1)))
        {
            writer.Write(pcm, 0, pcm.Length);
        }
        return stream.ToArray();
    }

    private async Task SendMessageAsync()
    {
        if (_sending) return;
        var text = ComposerInput.Text.Trim();
        if (text.Length == 0) return;
        _sending = true;
        ComposerInput.Text = "";
        AddBubble(text, true);
        AddBubble("…", false, true);
        try
        {
            using var response = await Http.PostAsJsonAsync($"{_baseUrl}/chat", new { message = text });
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode)
            {
                ReplacePending($"Ошибка сервера: HTTP {(int)response.StatusCode}");
                return;
            }
            using var doc = JsonDocument.Parse(body);
            var answer = doc.RootElement.TryGetProperty("answer", out var value) ? value.GetString() : null;
            var answerText = string.IsNullOrWhiteSpace(answer) ? "Пустой ответ." : answer!;
            ReplacePending(answerText);
            if (_autoSpeak) await SpeakAsync(answerText);
        }
        catch (Exception ex)
        {
            ReplacePending($"Не удалось связаться с Kivi: {ex.Message}");
        }
        finally
        {
            _sending = false;
        }
    }

    private void AddBubble(string text, bool fromUser, bool pending = false)
    {
        var content = new StackPanel();
        content.Children.Add(new TextBlock
        {
            Text = text,
            Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
            FontSize = 14,
            TextWrapping = TextWrapping.Wrap,
        });
        content.Children.Add(new TextBlock
        {
            Text = $"{(fromUser ? "Вы" : "Kivi")} · {DateTime.Now:HH:mm}",
            Foreground = new SolidColorBrush(Color.FromArgb(255, 143, 184, 176)),
            FontSize = 10,
            Margin = new Thickness(0, 6, 0, 0),
        });
        var bubble = new Border
        {
            Background = new SolidColorBrush(fromUser
                ? Color.FromArgb(255, 10, 58, 52)
                : Color.FromArgb(255, 8, 47, 43)),
            BorderBrush = new SolidColorBrush(Color.FromArgb(255, 22, 68, 61)),
            BorderThickness = new Thickness(fromUser ? 0 : 1),
            CornerRadius = new CornerRadius(fromUser ? 15 : 4, 15, 15, 15),
            Padding = new Thickness(16, 12, 16, 12),
            MaxWidth = 620,
            HorizontalAlignment = fromUser ? HorizontalAlignment.Right : HorizontalAlignment.Left,
            Child = content,
        };
        MessagesPanel.Children.Add(bubble);
        if (pending) _pendingBubble = bubble;
        MessagesScroll.ChangeView(null, MessagesScroll.ScrollableHeight, null);
    }

    private void ReplacePending(string text)
    {
        if (_pendingBubble?.Child is StackPanel content && content.Children[0] is TextBlock block)
        {
            block.Text = text;
            _pendingBubble = null;
        }
        MessagesScroll.ChangeView(null, MessagesScroll.ScrollableHeight, null);
    }
}
