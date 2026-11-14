using System.Diagnostics;
using System.IO;
using System.Media;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using NAudio.Wave;

namespace Kivi.Desktop;

public partial class MainWindow : Window
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromMinutes(5) };
    private string _baseUrl = "http://127.0.0.1:8765";
    private bool _sending;
    private Process? _ownedBackend;
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

    public MainWindow()
    {
        InitializeComponent();
        Loaded += OnLoaded;
        Closed += (_, _) =>
        {
            if (_ownedBackend is { HasExited: false }) _ownedBackend.Kill(entireProcessTree: true);
        };
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        var found = await FindApiAsync();
        if (!found)
        {
            ConnectionText.Text = "Запускаю локальный сервер…";
            await StartBackendAsync();
            found = await FindApiAsync(40);
        }
        ConnectionText.Text = found
            ? $"API: {_baseUrl.Replace("http://", "")}"
            : "API недоступен";
        if (found) await LoadHistoryAsync();
        SetActiveNav("chat");
    }

    private void SetActiveNav(string tag)
    {
        var inactive = Brushes.Transparent;
        var active = new SolidColorBrush(Color.FromRgb(11, 58, 52));
        foreach (var (button, key) in new[]
        {
            (ChatNav, "chat"),
            (VoicesNav, "voices"),
            (MediaNav, "media"),
            (FilesNav, "files"),
            (CrmNav, "crm"),
            (SettingsNav, "settings"),
        })
        {
            button.Background = key == tag ? active : inactive;
        }
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
                    // try next port / attempt
                }
            }
            if (attempt + 1 < attempts) await Task.Delay(500);
        }
        return false;
    }

    private async Task StartBackendAsync()
    {
        var root = FindProjectRoot();
        if (root is null) return;
        var pythonw = Path.Combine(root, "runtime", "pythonw.exe");
        if (!File.Exists(pythonw))
            pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(pythonw)) return;
        try
        {
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
        catch
        {
            _ownedBackend = null;
        }
        await Task.CompletedTask;
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

    private async void Nav_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button button) return;
        var tag = button.Tag?.ToString() ?? "chat";
        var labels = new Dictionary<string, string>
        {
            ["chat"] = "Чат",
            ["voices"] = "Голоса",
            ["media"] = "Медиа",
            ["files"] = "Файлы",
            ["crm"] = "CRM",
            ["settings"] = "Настройки",
        };
        PageTitle.Text = labels.GetValueOrDefault(tag, "Kivi");
        SetActiveNav(tag);
        ChatView.Visibility = tag == "chat" ? Visibility.Visible : Visibility.Collapsed;
        PageView.Visibility = tag == "chat" ? Visibility.Collapsed : Visibility.Visible;
        if (tag != "chat") await ShowPageAsync(tag);
    }

    private async Task LoadHistoryAsync()
    {
        using var doc = await GetJsonAsync("/history?limit=50");
        if (doc is null) return;
        if (!doc.RootElement.TryGetProperty("messages", out var messages)) return;
        MessagesPanel.Children.Clear();
        MessagesPanel.Children.Add(new TextBlock
        {
            Text = "ИСТОРИЯ",
            Foreground = (Brush)FindResource("Muted"),
            FontSize = 11,
            HorizontalAlignment = HorizontalAlignment.Center,
            Margin = new Thickness(0, 4, 0, 22),
        });
        foreach (var message in messages.EnumerateArray())
        {
            var role = message.TryGetProperty("role", out var r) ? r.GetString() : "";
            var content = message.TryGetProperty("content", out var c) ? c.GetString() : "";
            if (!string.IsNullOrWhiteSpace(content)) AddBubble(content!, role == "user");
        }
    }

    private async Task<JsonDocument?> GetJsonAsync(string path)
    {
        try
        {
            using var response = await Http.GetAsync(_baseUrl + path);
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode) return null;
            return JsonDocument.Parse(body);
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
            if (!response.IsSuccessStatusCode) return null;
            return JsonDocument.Parse(body);
        }
        catch
        {
            return null;
        }
    }

    private async Task ShowPageAsync(string tag)
    {
        PagePanel.Children.Clear();
        switch (tag)
        {
            case "voices": await BuildVoicesPageAsync(); break;
            case "settings": await BuildSettingsPageAsync(); break;
            case "media": BuildMediaPage(); break;
            case "files": await BuildFilesPageAsync(); break;
            case "crm": await BuildCrmPageAsync(); break;
        }
    }

    private TextBlock PageHeading(string text) => new()
    {
        Text = text,
        Foreground = (Brush)FindResource("Text"),
        FontSize = 24,
        FontWeight = FontWeights.SemiBold,
        Margin = new Thickness(0, 0, 0, 6),
    };

    private TextBlock PageSub(string text) => new()
    {
        Text = text,
        Foreground = (Brush)FindResource("Muted"),
        FontSize = 13,
        TextWrapping = TextWrapping.Wrap,
        Margin = new Thickness(0, 0, 0, 20),
    };

    private Button PageButton(string text, RoutedEventHandler click)
    {
        var button = new Button
        {
            Content = text,
            Style = (Style)FindResource("NavButton"),
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 0, 8, 8),
        };
        button.Click += click;
        return button;
    }

    private async Task BuildVoicesPageAsync()
    {
        PagePanel.Children.Add(PageHeading("Голоса"));
        PagePanel.Children.Add(PageSub("Выбери движок озвучивания. Активный голос хранится на сервере."));
        using var doc = await GetJsonAsync("/voices");
        if (doc is null)
        {
            PagePanel.Children.Add(PageSub("Не удалось загрузить голоса."));
            return;
        }
        if (!doc.RootElement.TryGetProperty("voices", out var voices)) return;
        foreach (var voice in voices.EnumerateArray())
        {
            var id = voice.TryGetProperty("id", out var i) ? i.GetString() ?? "" : "";
            var name = voice.TryGetProperty("name", out var n) ? n.GetString() ?? id : id;
            var engine = voice.TryGetProperty("engine", out var e) ? e.GetString() ?? "" : "";
            var active = voice.TryGetProperty("active", out var a) && a.GetBoolean();
            var available = !voice.TryGetProperty("available", out var av) || av.GetBoolean();
            var card = new Border
            {
                Background = (Brush)FindResource("Panel2"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(12),
                Padding = new Thickness(16, 12, 16, 12),
                Margin = new Thickness(0, 0, 0, 10),
            };
            var row = new DockPanel();
            var info = new StackPanel();
            info.Children.Add(new TextBlock { Text = name, Foreground = (Brush)FindResource("Text"), FontSize = 15 });
            info.Children.Add(new TextBlock
            {
                Text = engine,
                Foreground = (Brush)FindResource("Muted"),
                FontSize = 11,
                Margin = new Thickness(0, 3, 0, 0),
            });
            row.Children.Add(info);
            var choose = PageButton(active ? "Активен" : available ? "Выбрать" : "Недоступен", async (_, _) =>
            {
                if (!available) return;
                using var result = await PostJsonAsync("/voices/active", new { id });
                await ShowPageAsync("voices");
            });
            choose.IsEnabled = available && !active;
            DockPanel.SetDock(choose, Dock.Right);
            row.Children.Add(choose);
            card.Child = row;
            PagePanel.Children.Add(card);
        }
    }

    private async Task BuildSettingsPageAsync()
    {
        PagePanel.Children.Add(PageHeading("Настройки"));
        PagePanel.Children.Add(PageSub("Голосовой движок и параметры локального сервера."));
        using var doc = await GetJsonAsync("/settings");
        if (doc is null)
        {
            PagePanel.Children.Add(PageSub("Не удалось загрузить настройки."));
            return;
        }
        var providers = new[] { "qwen", "silero", "piper", "fish" };
        var combo = new ComboBox
        {
            Width = 260,
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 0, 0, 12),
        };
        foreach (var provider in providers) combo.Items.Add(provider);
        var current = doc.RootElement.TryGetProperty("voice_provider", out var p) ? p.GetString() : "qwen";
        combo.SelectedItem = providers.Contains(current) ? current : "qwen";
        PagePanel.Children.Add(combo);
        PagePanel.Children.Add(PageButton("Сохранить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/settings", new { voice_provider = combo.SelectedItem?.ToString() });
            await ShowPageAsync("settings");
        }));
        foreach (var key in new[] { "model", "speaker", "sample_rate", "fish_model" })
        {
            if (!doc.RootElement.TryGetProperty(key, out var value)) continue;
            PagePanel.Children.Add(new TextBlock
            {
                Text = $"{key}: {value}",
                Foreground = (Brush)FindResource("Muted"),
                FontSize = 12,
                Margin = new Thickness(0, 4, 0, 0),
            });
        }
    }

    private void BuildMediaPage()
    {
        PagePanel.Children.Add(PageHeading("Медиа"));
        PagePanel.Children.Add(PageSub("Локальная генерация изображений и видео через текущий Kivi API."));
        var prompt = new TextBox
        {
            MinHeight = 58,
            TextWrapping = TextWrapping.Wrap,
            AcceptsReturn = true,
            Background = (Brush)FindResource("Panel2"),
            Foreground = (Brush)FindResource("Text"),
            BorderBrush = (Brush)FindResource("Line"),
            Padding = new Thickness(10),
            Margin = new Thickness(0, 0, 0, 10),
        };
        PagePanel.Children.Add(prompt);
        var output = new StackPanel();
        PagePanel.Children.Add(PageButton("Сгенерировать изображение", async (_, _) =>
        {
            output.Children.Clear();
            output.Children.Add(PageSub("Генерирую…"));
            using var result = await PostJsonAsync("/media/image", new
            {
                prompt = prompt.Text,
                width = 512,
                height = 512,
                model = "flux",
            });
            output.Children.Clear();
            if (result is null)
            {
                output.Children.Add(PageSub("Генерация недоступна."));
                return;
            }
            var image = result.RootElement.TryGetProperty("image", out var value) ? value.GetString() : null;
            if (image is not null && image.StartsWith("data:image/", StringComparison.Ordinal))
            {
                var comma = image.IndexOf(',');
                if (comma > 0) output.Children.Add(BuildImage(Convert.FromBase64String(image[(comma + 1)..])));
            }
        }));
        PagePanel.Children.Add(PageButton("Сгенерировать видео 4 с", async (_, _) =>
        {
            output.Children.Clear();
            output.Children.Add(PageSub("Генерирую видео…"));
            using var result = await PostJsonAsync("/media/video", new
            {
                prompt = prompt.Text,
                duration = 4,
                model = "local",
            });
            output.Children.Clear();
            if (result is null)
            {
                output.Children.Add(PageSub("Видео недоступно."));
                return;
            }
            var video = result.RootElement.TryGetProperty("video", out var value) ? value.GetString() : null;
            if (video is null || !video.StartsWith("data:video/", StringComparison.Ordinal))
            {
                output.Children.Add(PageSub("Сервер вернул не видео."));
                return;
            }
            var comma = video.IndexOf(',');
            if (comma <= 0) return;
            var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"kivi-{Guid.NewGuid():N}.mp4");
            await File.WriteAllBytesAsync(path, Convert.FromBase64String(video[(comma + 1)..]));
            var player = new MediaElement
            {
                Source = new Uri(path),
                Width = 520,
                Height = 292,
                LoadedBehavior = MediaState.Manual,
                UnloadedBehavior = MediaState.Manual,
                Margin = new Thickness(0, 12, 0, 0),
            };
            player.MediaEnded += (_, _) => player.Position = TimeSpan.Zero;
            output.Children.Add(player);
            player.Play();
        }));
        PagePanel.Children.Add(output);
    }

    private static Image BuildImage(byte[] bytes)
    {
        var bitmap = new System.Windows.Media.Imaging.BitmapImage();
        using var stream = new MemoryStream(bytes);
        bitmap.BeginInit();
        bitmap.CacheOption = System.Windows.Media.Imaging.BitmapCacheOption.OnLoad;
        bitmap.StreamSource = stream;
        bitmap.EndInit();
        bitmap.Freeze();
        return new Image
        {
            Source = bitmap,
            MaxWidth = 420,
            Stretch = Stretch.Uniform,
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 12, 0, 0),
        };
    }

    private string _nativeFilesPath = ".";
    private string _nativeFilesView = ".";
    private readonly Stack<string> _nativeFilesBack = new();
    private TextBox? _nativePathBox;
    private TextBox? _nativeSearchBox;
    private StackPanel? _nativeFilesList;
    private StackPanel? _nativePreview;
    private TextBlock? _nativeFilesStatus;
    private readonly List<NativeEntry> _nativeEntries = new();
    private string _nativeSort = "name";

    private async Task BuildFilesPageAsync()
    {
        PagePanel.Children.Add(PageHeading("Файлы"));
        PagePanel.Children.Add(PageSub("Нативный файловый менеджер Kivi: рабочая папка, известные папки и диски."));

        var toolbar = new WrapPanel { Margin = new Thickness(0, 0, 0, 12) };
        toolbar.Children.Add(PageButton("Компьютер", async (_, _) =>
        {
            _nativeFilesBack.Push(_nativeFilesPath);
            await LoadNativeFilesAsync("shell://");
        }));
        toolbar.Children.Add(PageButton("Назад", async (_, _) =>
        {
            if (_nativeFilesBack.Count > 0) await LoadNativeFilesAsync(_nativeFilesBack.Pop());
        }));
        toolbar.Children.Add(PageButton("Вверх", async (_, _) =>
        {
            var parent = NativeParent(_nativeFilesView);
            if (parent is not null) await LoadNativeFilesAsync(parent);
        }));
        toolbar.Children.Add(PageButton("Новая папка", async (_, _) => await NativeMkdirAsync()));
        toolbar.Children.Add(PageButton("Отменить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/fs/undo", new { });
            if (result is not null) await LoadNativeFilesAsync(_nativeFilesPath, false);
        }));
        PagePanel.Children.Add(toolbar);

        var pathRow = new DockPanel { Margin = new Thickness(0, 0, 0, 8) };
        _nativePathBox = new TextBox
        {
            Text = _nativeFilesPath,
            Background = (Brush)FindResource("Panel2"),
            Foreground = (Brush)FindResource("Text"),
            BorderBrush = (Brush)FindResource("Line"),
            Padding = new Thickness(10, 8, 10, 8),
            FontSize = 13,
        };
        var go = PageButton("Перейти", async (_, _) =>
        {
            var target = (_nativePathBox?.Text ?? ".").Trim();
            if (target.Length > 0) await LoadNativeFilesAsync(target);
        });
        go.Margin = new Thickness(8, 0, 0, 0);
        DockPanel.SetDock(go, Dock.Right);
        pathRow.Children.Add(go);
        pathRow.Children.Add(_nativePathBox);
        PagePanel.Children.Add(pathRow);

        var searchRow = new DockPanel { Margin = new Thickness(0, 0, 0, 10) };
        _nativeSearchBox = new TextBox
        {
            Background = (Brush)FindResource("Panel2"),
            Foreground = (Brush)FindResource("Text"),
            BorderBrush = (Brush)FindResource("Line"),
            Padding = new Thickness(10, 8, 10, 8),
            FontSize = 13,
        };
        var searchButton = PageButton("Найти", async (_, _) => await NativeSearchAsync());
        searchButton.Margin = new Thickness(8, 0, 0, 0);
        DockPanel.SetDock(searchButton, Dock.Right);
        var clearButton = PageButton("Очистить", async (_, _) =>
        {
            if (_nativeSearchBox is not null) _nativeSearchBox.Text = "";
            await LoadNativeFilesAsync(_nativeFilesPath, false);
        });
        clearButton.Margin = new Thickness(8, 0, 0, 0);
        DockPanel.SetDock(clearButton, Dock.Right);
        searchRow.Children.Add(searchButton);
        searchRow.Children.Add(clearButton);
        searchRow.Children.Add(_nativeSearchBox);
        PagePanel.Children.Add(searchRow);

        var sortRow = new DockPanel { Margin = new Thickness(0, 0, 0, 10) };
        sortRow.Children.Add(new TextBlock
        {
            Text = "Сортировка:",
            Foreground = (Brush)FindResource("Muted"),
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 8, 0),
        });
        var sort = new ComboBox { Width = 180 };
        sort.Items.Add("По имени");
        sort.Items.Add("По размеру");
        sort.Items.Add("По типу");
        sort.SelectedIndex = _nativeSort switch { "size" => 1, "type" => 2, _ => 0 };
        sort.SelectionChanged += (_, _) =>
        {
            _nativeSort = sort.SelectedIndex switch { 1 => "size", 2 => "type", _ => "name" };
            RenderNativeEntries();
        };
        sortRow.Children.Add(sort);
        PagePanel.Children.Add(sortRow);

        _nativeFilesStatus = PageSub("Загружаю…");
        PagePanel.Children.Add(_nativeFilesStatus);

        var body = new Grid { Margin = new Thickness(0, 4, 0, 0) };
        body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(3, GridUnitType.Star) });
        body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(2, GridUnitType.Star) });
        var scroller = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, MaxHeight = 620 };
        _nativeFilesList = new StackPanel();
        scroller.Content = _nativeFilesList;
        body.Children.Add(scroller);
        var previewScroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Margin = new Thickness(12, 0, 0, 0) };
        _nativePreview = new StackPanel();
        previewScroll.Content = _nativePreview;
        Grid.SetColumn(previewScroll, 1);
        body.Children.Add(previewScroll);
        PagePanel.Children.Add(body);
        await LoadNativeFilesAsync(_nativeFilesPath);
    }

    private async Task LoadNativeFilesAsync(string path, bool record = true)
    {
        if (_nativeFilesList is null || _nativeFilesStatus is null) return;
        if (record && _nativeFilesPath != path) _nativeFilesBack.Push(_nativeFilesPath);
        _nativeFilesPath = path;
        if (_nativePathBox is not null) _nativePathBox.Text = path;
        _nativeFilesList.Children.Clear();
        _nativeFilesStatus.Text = "Загружаю…";
        using var doc = await GetJsonAsync($"/fs/list?path={Uri.EscapeDataString(path)}");
        if (doc is null)
        {
            _nativeFilesStatus.Text = "Не удалось открыть папку.";
            return;
        }
        _nativeFilesView = doc.RootElement.TryGetProperty("path", out var resolved)
            ? resolved.GetString() ?? path
            : path;
        if (!doc.RootElement.TryGetProperty("entries", out var entries))
        {
            _nativeFilesStatus.Text = "Пустая папка.";
            return;
        }
        var count = 0;
        _nativeEntries.Clear();
        foreach (var entry in entries.EnumerateArray())
        {
            count++;
            var name = entry.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
            var type = entry.TryGetProperty("type", out var t) ? t.GetString() ?? "file" : "file";
            var entryPath = entry.TryGetProperty("path", out var ep) ? ep.GetString() : null;
            entryPath ??= NativeJoin(_nativeFilesView, name);
            var size = entry.TryGetProperty("size", out var s) && s.TryGetInt64(out var bytes) ? bytes : 0;
            _nativeEntries.Add(new NativeEntry(name, type, entryPath, size));
        }
        RenderNativeEntries();
        _nativeFilesStatus.Text = count == 0 ? "Папка пуста." : $"{count} объектов · {_nativeFilesView}";
        if (_nativePreview is not null) _nativePreview.Children.Clear();
    }

    private void RenderNativeEntries()
    {
        if (_nativeFilesList is null) return;
        _nativeFilesList.Children.Clear();
        IEnumerable<NativeEntry> ordered = _nativeSort switch
        {
            "size" => _nativeEntries.OrderByDescending(item => item.Size),
            "type" => _nativeEntries.OrderBy(item => item.Type).ThenBy(item => item.Name),
            _ => _nativeEntries.OrderBy(item => item.Name, StringComparer.CurrentCultureIgnoreCase),
        };
        foreach (var entry in ordered)
            _nativeFilesList.Children.Add(BuildNativeFileRow(entry.Name, entry.Type, entry.Path, entry.Size));
    }

    private FrameworkElement BuildNativeFileRow(string name, string type, string path, long size)
    {
        var row = new Border
        {
            Background = (Brush)FindResource("Panel2"),
            BorderBrush = (Brush)FindResource("Line"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(9),
            Padding = new Thickness(10, 8, 10, 8),
            Margin = new Thickness(0, 0, 6, 6),
            Cursor = System.Windows.Input.Cursors.Hand,
        };
        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var iconHost = new Border
        {
            Width = 34,
            Height = 34,
            CornerRadius = new CornerRadius(8),
            Background = new SolidColorBrush(Color.FromRgb(10, 55, 49)),
            Margin = new Thickness(0, 0, 10, 0),
            VerticalAlignment = VerticalAlignment.Center,
        };
        var icon = new TextBlock
        {
            Text = type == "dir" ? "▸" : "·",
            Foreground = (Brush)FindResource(type == "dir" ? "Mint" : "Muted"),
            FontSize = 18,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        iconHost.Child = icon;
        grid.Children.Add(iconHost);
        if (type != "dir") _ = LoadNativeThumbAsync(iconHost, path);
        var title = new TextBlock
        {
            Text = name,
            Foreground = (Brush)FindResource("Text"),
            FontSize = 13.5,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis,
        };
        Grid.SetColumn(title, 1);
        grid.Children.Add(title);
        var meta = new TextBlock
        {
            Text = type == "dir" ? "папка" : size > 0 ? $"{size / 1024.0:0.#} КБ" : "файл",
            Foreground = (Brush)FindResource("Muted"),
            FontSize = 10.5,
            Margin = new Thickness(8, 0, 0, 0),
            VerticalAlignment = VerticalAlignment.Center,
        };
        Grid.SetColumn(meta, 2);
        grid.Children.Add(meta);
        row.Child = grid;

        row.MouseLeftButtonUp += async (_, _) =>
        {
            if (type == "dir") await LoadNativeFilesAsync(path);
            else await ShowNativePreviewAsync(path, name);
        };
        var menu = new ContextMenu();
        var openExplorer = new MenuItem { Header = "Открыть в Проводнике" };
        openExplorer.Click += async (_, _) => await PostJsonAsync("/fs/reveal", new { path });
        var rename = new MenuItem { Header = "Переименовать" };
        rename.Click += async (_, _) => await NativeRenameAsync(path, name);
        var copy = new MenuItem { Header = "Копировать в…" };
        copy.Click += async (_, _) => await NativeTransferAsync(path, name, "copy");
        var move = new MenuItem { Header = "Переместить в…" };
        move.Click += async (_, _) => await NativeTransferAsync(path, name, "move");
        var trash = new MenuItem { Header = "В корзину" };
        trash.Click += async (_, _) => await NativeTrashAsync(path, name);
        menu.Items.Add(openExplorer);
        menu.Items.Add(rename);
        menu.Items.Add(copy);
        menu.Items.Add(move);
        menu.Items.Add(trash);
        row.ContextMenu = menu;
        return row;
    }

    private async Task LoadNativeThumbAsync(Border host, string path)
    {
        try
        {
            var bytes = await Http.GetByteArrayAsync(
                $"{_baseUrl}/fs/thumb?path={Uri.EscapeDataString(path)}");
            var bitmap = new System.Windows.Media.Imaging.BitmapImage();
            using var stream = new MemoryStream(bytes);
            bitmap.BeginInit();
            bitmap.CacheOption = System.Windows.Media.Imaging.BitmapCacheOption.OnLoad;
            bitmap.StreamSource = stream;
            bitmap.EndInit();
            bitmap.Freeze();
            host.Child = new Image
            {
                Source = bitmap,
                Stretch = Stretch.UniformToFill,
                Width = 34,
                Height = 34,
            };
        }
        catch
        {
            // keep the type glyph when no thumbnail is available
        }
    }

    private async Task NativeSearchAsync()
    {
        if (_nativeSearchBox is null || _nativeFilesList is null || _nativeFilesStatus is null) return;
        var query = _nativeSearchBox.Text.Trim();
        if (query.Length == 0)
        {
            await LoadNativeFilesAsync(_nativeFilesPath, false);
            return;
        }
        _nativeFilesList.Children.Clear();
        _nativeFilesStatus.Text = "Ищу…";
        using var doc = await GetJsonAsync(
            $"/fs/search?q={Uri.EscapeDataString(query)}&path={Uri.EscapeDataString(_nativeFilesView)}");
        if (doc is null)
        {
            _nativeFilesStatus.Text = "Поиск не удался.";
            return;
        }
        var count = 0;
        if (doc.RootElement.TryGetProperty("matches", out var matches))
        {
            foreach (var entry in matches.EnumerateArray())
            {
                count++;
                var name = entry.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                var type = entry.TryGetProperty("type", out var t) ? t.GetString() ?? "file" : "file";
                var path = entry.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
                if (path.Length == 0) continue;
                _nativeFilesList.Children.Add(BuildNativeFileRow(name, type, path, 0));
            }
        }
        _nativeFilesStatus.Text = count == 0 ? "Ничего не найдено." : $"{count} результатов поиска.";
    }

    private async Task NativeTransferAsync(string source, string name, string mode)
    {
        var destination = Prompt(mode == "copy" ? "Копировать" : "Переместить", "Куда (полный путь)", _nativeFilesView);
        if (string.IsNullOrWhiteSpace(destination)) return;
        var target = NativeJoin(destination.Trim(), name);
        using var result = await PostJsonAsync(
            mode == "copy" ? "/fs/copy" : "/fs/move",
            new { source, destination = target });
        await LoadNativeFilesAsync(_nativeFilesPath, false);
    }

    private async Task ShowNativePreviewAsync(string path, string name)
    {
        if (_nativePreview is null) return;
        _nativePreview.Children.Clear();
        _nativePreview.Children.Add(new TextBlock
        {
            Text = name,
            Foreground = (Brush)FindResource("Text"),
            FontSize = 17,
            FontWeight = FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 8),
        });
        var suffix = System.IO.Path.GetExtension(name).ToLowerInvariant();
        if (new[] { ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp" }.Contains(suffix))
        {
            try
            {
                var bytes = await Http.GetByteArrayAsync($"{_baseUrl}/fs/raw?path={Uri.EscapeDataString(path)}");
                using var doc = JsonDocument.Parse(bytes);
                var data = doc.RootElement.GetProperty("image").GetString() ?? "";
                var comma = data.IndexOf(',');
                if (comma > 0) _nativePreview.Children.Add(BuildImage(Convert.FromBase64String(data[(comma + 1)..])));
            }
            catch
            {
                _nativePreview.Children.Add(PageSub("Не удалось показать изображение."));
            }
            return;
        }
        using var textDoc = await GetJsonAsync($"/fs/read?path={Uri.EscapeDataString(path)}");
        if (textDoc is null)
        {
            _nativePreview.Children.Add(PageSub("Предпросмотр недоступен."));
            return;
        }
        var content = textDoc.RootElement.TryGetProperty("content", out var value) ? value.GetString() : "";
        _nativePreview.Children.Add(new TextBox
        {
            Text = content,
            IsReadOnly = true,
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            Background = (Brush)FindResource("Panel"),
            Foreground = (Brush)FindResource("Text"),
            BorderThickness = new Thickness(0),
            Padding = new Thickness(8),
            MaxHeight = 520,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        });
    }

    private async Task NativeMkdirAsync()
    {
        var name = Prompt("Новая папка", "Имя папки");
        if (string.IsNullOrWhiteSpace(name)) return;
        var path = NativeJoin(_nativeFilesView, name.Trim());
        using var result = await PostJsonAsync("/fs/mkdir", new { path });
        await LoadNativeFilesAsync(_nativeFilesPath, false);
    }

    private async Task NativeRenameAsync(string path, string oldName)
    {
        var name = Prompt("Переименовать", "Новое имя", oldName);
        if (string.IsNullOrWhiteSpace(name) || name == oldName) return;
        var destination = NativeJoin(NativeParent(path) ?? _nativeFilesView, name.Trim());
        using var result = await PostJsonAsync("/fs/rename", new { source = path, destination });
        await LoadNativeFilesAsync(_nativeFilesPath, false);
    }

    private async Task NativeTrashAsync(string path, string name)
    {
        if (MessageBox.Show($"Переместить «{name}» в Корзину?", "Kivi", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes)
            return;
        using var result = await PostJsonAsync("/fs/trash", new { path, confirm = name });
        await LoadNativeFilesAsync(_nativeFilesPath, false);
    }

    private static string? NativeParent(string path)
    {
        if (path == "shell://" || path == ".") return null;
        var trimmed = path.TrimEnd('/', '\\');
        if (trimmed.Length == 2 && trimmed[1] == ':') return "shell://";
        var index = Math.Max(trimmed.LastIndexOf('/'), trimmed.LastIndexOf('\\'));
        if (index <= 0) return ".";
        var parent = trimmed[..index];
        return parent.Length == 2 && parent[1] == ':' ? parent + "/" : parent;
    }

    private static string NativeJoin(string directory, string name)
    {
        if (directory == "shell://") return name;
        if (directory.EndsWith('/') || directory.EndsWith('\\')) return directory + name;
        return directory + "/" + name;
    }

    private static string Prompt(string title, string label, string initial = "")
    {
        var window = new Window
        {
            Title = title,
            Width = 420,
            Height = 170,
            WindowStartupLocation = WindowStartupLocation.CenterOwner,
            ResizeMode = ResizeMode.NoResize,
            Background = new SolidColorBrush(Color.FromRgb(4, 31, 29)),
            Foreground = new SolidColorBrush(Color.FromRgb(228, 247, 241)),
            Owner = Application.Current.MainWindow,
        };
        var panel = new StackPanel { Margin = new Thickness(18) };
        panel.Children.Add(new TextBlock { Text = label, Margin = new Thickness(0, 0, 0, 8) });
        var box = new TextBox { Text = initial, Padding = new Thickness(8) };
        panel.Children.Add(box);
        var ok = new Button { Content = "OK", Width = 80, Margin = new Thickness(0, 12, 0, 0), HorizontalAlignment = HorizontalAlignment.Right };
        ok.Click += (_, _) => window.DialogResult = true;
        panel.Children.Add(ok);
        window.Content = panel;
        window.Loaded += (_, _) => box.Focus();
        return window.ShowDialog() == true ? box.Text : "";
    }


    private async Task BuildCrmPageAsync()
    {
        PagePanel.Children.Add(PageHeading("CRM"));
        PagePanel.Children.Add(PageSub("Статус AltCRM, read-only карточка и формы без сохранения."));
        using var doc = await GetJsonAsync("/crm/status");
        var connected = doc?.RootElement.TryGetProperty("connected", out var c) == true && c.GetBoolean();
        PagePanel.Children.Add(PageSub(connected ? "Расширение подключено." : "Расширение не подключено."));
        PagePanel.Children.Add(PageButton("Прочитать заказ", async (_, _) =>
        {
            using var result = await GetJsonAsync("/crm/read");
            if (result is null) return;
            PagePanel.Children.Add(PageSub(result.RootElement.ToString()));
        }));

        PagePanel.Children.Add(PageSub("Заполнить карточку (без сохранения)"));
        var comment = NativeInput("Комментарий");
        var deadline = NativeInput("Согласованные сроки ремонта");
        PagePanel.Children.Add(comment);
        PagePanel.Children.Add(deadline);
        PagePanel.Children.Add(PageButton("Заполнить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/crm/fill", new
            {
                comment = comment.Text,
                checklist = new Dictionary<string, string>
                {
                    ["deadline"] = deadline.Text,
                },
            });
            PagePanel.Children.Add(PageSub(result is null ? "Не удалось заполнить." : "Заполнено, не сохранено."));
        }));

        PagePanel.Children.Add(PageSub("Добавить строку акта"));
        var work = NativeInput("Работа");
        var price = NativeInput("Цена, ₽");
        var warranty = NativeInput("Гарантия, дней");
        PagePanel.Children.Add(work);
        PagePanel.Children.Add(price);
        PagePanel.Children.Add(warranty);
        PagePanel.Children.Add(PageButton("Добавить", async (_, _) =>
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
            PagePanel.Children.Add(PageSub(result is null ? "Не удалось добавить." : "Строка добавлена, не сохранено."));
        }));

        PagePanel.Children.Add(PageSub("Черновик задачи"));
        var taskComment = NativeInput("Текст задачи");
        var taskWhen = NativeInput("Срок, например завтра");
        PagePanel.Children.Add(taskComment);
        PagePanel.Children.Add(taskWhen);
        PagePanel.Children.Add(PageButton("Заполнить задачу", async (_, _) =>
        {
            using var result = await PostJsonAsync("/crm/task", new
            {
                comment = taskComment.Text,
                when_text = taskWhen.Text,
            });
            PagePanel.Children.Add(PageSub(result is null ? "Не удалось заполнить." : "Форма задачи заполнена, не создана."));
        }));
    }

    private TextBox NativeInput(string placeholder) => new()
    {
        Text = placeholder,
        Foreground = (Brush)FindResource("Muted"),
        Background = (Brush)FindResource("Panel2"),
        BorderBrush = (Brush)FindResource("Line"),
        Padding = new Thickness(8),
        Margin = new Thickness(0, 0, 0, 8),
        MinWidth = 320,
        HorizontalAlignment = HorizontalAlignment.Left,
    };

    private void ComposerInput_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter && Keyboard.Modifiers != ModifierKeys.Shift)
        {
            e.Handled = true;
            _ = SendMessageAsync();
        }
    }

    private void Send_Click(object sender, RoutedEventArgs e) => _ = SendMessageAsync();

    private async void Speak_Click(object sender, RoutedEventArgs e)
    {
        _autoSpeak = !_autoSpeak;
        SpeakButton.Background = new SolidColorBrush(
            _autoSpeak ? Color.FromRgb(21, 83, 72) : Colors.Transparent);
        await Task.CompletedTask;
    }

    private async void Mic_Click(object sender, RoutedEventArgs e)
    {        if (_recording)
        {
            await StopRecordingAsync();
            return;
        }
        try
        {
            _micStream = new MemoryStream();
            _micWriter = new WaveFileWriter(_micStream, new WaveFormat(16000, 16, 1));
            _waveIn = new WaveInEvent
            {
                WaveFormat = new WaveFormat(16000, 16, 1),
                BufferMilliseconds = 100,
            };
            _waveIn.DataAvailable += (_, args) =>
            {
                _micWriter?.Write(args.Buffer, 0, args.BytesRecorded);
                _micWriter?.Flush();
            };
            _waveIn.StartRecording();
            _recording = true;
            MicButton.Background = new SolidColorBrush(Color.FromRgb(21, 83, 72));
            ComposerInput.Text = "";
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
        MicButton.Background = Brushes.Transparent;
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

    private async void Wake_Click(object sender, RoutedEventArgs e)
    {
        if (_wakeMode)
        {
            StopWakeMode();
            return;
        }
        if (_recording) await StopRecordingAsync();
        try
        {
            _wakeMode = true;
            lock (_wakeLock) _wakePcm.Clear();
            _wakeIn = new WaveInEvent
            {
                WaveFormat = new WaveFormat(16000, 16, 1),
                BufferMilliseconds = 200,
            };
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
            WakeButton.Background = new SolidColorBrush(Color.FromRgb(21, 83, 72));
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
                var detected = doc.RootElement.TryGetProperty("detected", out var value) && value.GetBoolean();
                if (!detected) continue;
                StopWakeMode();
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
                // a wake probe failure is not fatal; keep listening
            }
            finally
            {
                _wakeBusy = false;
            }
        }
    }

    private void StopWakeMode()
    {
        _wakeMode = false;
        WakeButton.Background = Brushes.Transparent;
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

    private async Task SpeakTextAsync(string text)
    {
        try
        {
            using var response = await Http.PostAsJsonAsync($"{_baseUrl}/tts", new { text });
            if (!response.IsSuccessStatusCode) return;
            var audio = await response.Content.ReadAsByteArrayAsync();
            await Task.Run(() =>
            {
                using var stream = new MemoryStream(audio);
                using var player = new SoundPlayer(stream);
                player.PlaySync();
            });
        }
        catch
        {
            // speech is best-effort
        }
    }

    private async Task SendMessageAsync()
    {
        if (_sending) return;
        var text = ComposerInput.Text.Trim();
        if (text.Length == 0) return;
        _sending = true;
        ComposerInput.Clear();
        AddBubble(text, fromUser: true);
        AddBubble("…", fromUser: false, pending: true);
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
            var answer = doc.RootElement.TryGetProperty("answer", out var value)
                ? value.GetString()
                : null;
            var answerText = string.IsNullOrWhiteSpace(answer) ? "Пустой ответ." : answer!;
            ReplacePending(answerText);
            if (_autoSpeak) await SpeakTextAsync(answerText);
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

    private Border? _pendingBubble;

    private void AddBubble(string text, bool fromUser, bool pending = false)
    {
        var content = new TextBlock
        {
            Text = text,
            Foreground = (Brush)FindResource("Text"),
            FontSize = 14,
            TextWrapping = TextWrapping.Wrap,
        };
        var border = new Border
        {
            Background = new SolidColorBrush(fromUser ? Color.FromRgb(10, 58, 52) : Color.FromRgb(8, 47, 43)),
            BorderBrush = (Brush)FindResource("Line"),
            BorderThickness = fromUser ? new Thickness(0) : new Thickness(1),
            CornerRadius = new CornerRadius(fromUser ? 15 : 4, 15, 15, 15),
            Padding = new Thickness(16, 12, 16, 12),
            Margin = new Thickness(fromUser ? 120 : 0, 0, fromUser ? 0 : 120, 18),
            HorizontalAlignment = fromUser ? HorizontalAlignment.Right : HorizontalAlignment.Left,
            MaxWidth = 620,
            Child = content,
        };
        MessagesPanel.Children.Add(border);
        if (pending) _pendingBubble = border;
        MessagesScroll.ScrollToEnd();
    }

    private void ReplacePending(string text)
    {
        if (_pendingBubble?.Child is TextBlock block)
        {
            block.Text = text;
            _pendingBubble = null;
        }
        MessagesScroll.ScrollToEnd();
    }

    private sealed record NativeEntry(string Name, string Type, string Path, long Size);
}
