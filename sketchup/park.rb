# encoding: UTF-8
# Генератор парковой зоны для SketchUp 2023 (Ruby 2.7)
# Участок 60 x 40 м: фонтан (центр), беседка (СВ), детская площадка (ЮВ),
# аллея со входом слева, цветники, деревья, скамейки, фонари.
# Запуск: Window > Ruby Console, затем:
#   load 'D:/PROJECT_INNOTECH/SVN/sketchup/park.rb'
# Запускать в ПУСТОЙ модели (File > New). Отмена одним Ctrl+Z.

require 'sketchup.rb'

module ParkGen
  Z      = Geom::Vector3d.new(0, 0, 1)
  TWO_PI = Math::PI * 2

  # ---------- примитивы (всё в метрах) ----------

  def self.pt(x, y, z)
    Geom::Point3d.new(x.m, y.m, z.m)
  end

  def self.mats(model)
    defs = {
      grass: [92, 145, 70], path: [190, 185, 175], plaza: [220, 215, 205],
      stone: [150, 150, 155], dark: [105, 105, 112], wood: [150, 100, 60],
      roof: [125, 62, 52], rubber: [190, 80, 60], rubber2: [60, 110, 170],
      sand: [228, 208, 150], trunk: [100, 70, 45], leaf: [60, 125, 55],
      leaf2: [88, 152, 62], conifer: [35, 95, 60], hedge: [48, 108, 50],
      metal: [70, 70, 75], yellow: [255, 210, 90], soil: [85, 60, 40],
      white: [240, 240, 240]
    }
    h = {}
    defs.each do |k, rgb|
      m = model.materials.add("park_#{k}")
      m.color = Sketchup::Color.new(*rgb)
      h[k] = m
    end
    w = model.materials.add('park_water')
    w.color = Sketchup::Color.new(90, 170, 220)
    w.alpha = 0.55
    h[:water] = w
    %w[red pink white yellow].zip([[220, 50, 60], [240, 130, 180], [245, 245, 245], [250, 215, 60]]).each do |n, rgb|
      m = model.materials.add("park_flower_#{n}")
      m.color = Sketchup::Color.new(*rgb)
      h["f_#{n}".to_sym] = m
    end
    h
  end

  def self.paint(g, m)
    g.material = m if m
    g
  end

  # многоугольник (x,y) выдавливается вверх на h
  def self.prism(ents, pts2, z, h, m)
    g = ents.add_group
    face = g.entities.add_face(pts2.map { |x, y| pt(x, y, z) })
    face.reverse! if face.normal.z < 0
    face.pushpull(h.m)
    paint(g, m)
  end

  def self.box(ents, x, y, z, w, d, h, m)
    prism(ents, [[x, y], [x + w, y], [x + w, y + d], [x, y + d]], z, h, m)
  end

  # профиль в плоскости XZ, толщина по Y
  def self.prism_xz(ents, pts, y0, depth, m)
    g = ents.add_group
    face = g.entities.add_face(pts.map { |x, z| pt(x, y0, z) })
    face.reverse! if face.normal.y < 0
    face.pushpull(depth.m)
    paint(g, m)
  end

  def self.cyl(ents, cx, cy, z, r, h, m, seg = 24)
    g = ents.add_group
    edges = g.entities.add_circle(pt(cx, cy, z), Z, r.m, seg)
    face = g.entities.add_face(edges)
    face.reverse! if face.normal.z < 0
    face.pushpull(h.m)
    paint(g, m)
  end

  # кольцо (стенка чаши): внешний/внутренний радиус
  def self.ring(ents, cx, cy, z, r_out, r_in, h, m, seg = 48)
    g = ents.add_group
    c = pt(cx, cy, z)
    g.entities.add_face(g.entities.add_circle(c, Z, r_out.m, seg))
    g.entities.add_circle(c, Z, r_in.m, seg)
    faces = g.entities.grep(Sketchup::Face).sort_by(&:area)
    faces.first.erase!
    ring_face = g.entities.grep(Sketchup::Face).first
    ring_face.reverse! if ring_face.normal.z < 0
    ring_face.pushpull(h.m)
    paint(g, m)
  end

  def self.strip(ents, a, b, width, z, h, m)
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    len = Math.hypot(dx, dy)
    nx = -dy / len * width / 2.0
    ny = dx / len * width / 2.0
    prism(ents, [[a[0] + nx, a[1] + ny], [b[0] + nx, b[1] + ny],
                 [b[0] - nx, b[1] - ny], [a[0] - nx, a[1] - ny]], z, h, m)
  end

  def self.ngon(cx, cy, r, n, rot = 0)
    (0...n).map { |i| a = TWO_PI * i / n + rot; [cx + r * Math.cos(a), cy + r * Math.sin(a)] }
  end

  def self.pyramid(ents, cx, cy, z, r, h, n, m, rot = 0)
    g = ents.add_group
    base = ngon(cx, cy, r, n, rot).map { |x, y| pt(x, y, z) }
    apex = pt(cx, cy, z + h)
    n.times { |i| g.entities.add_face(base[i], base[(i + 1) % n], apex) }
    g.entities.add_face(base)
    paint(g, m)
  end

  def self.sphere(ents, cx, cy, cz, r, m, lat = 6, lon = 12)
    g = ents.add_group
    rings = (0..lat).map { |i| phi = Math::PI * i / lat; [r * Math.cos(phi), r * Math.sin(phi)] }
    p = lambda do |ri, a|
      zz, rad = rings[ri]
      pt(cx + rad * Math.cos(a), cy + rad * Math.sin(a), cz + zz)
    end
    lat.times do |i|
      lon.times do |j|
        a0 = TWO_PI * j / lon
        a1 = TWO_PI * (j + 1) / lon
        pts =
          if i == 0 then [p.(i, a0), p.(i + 1, a1), p.(i + 1, a0)]
          elsif i == lat - 1 then [p.(i, a0), p.(i, a1), p.(i + 1, a0)]
          else [p.(i, a0), p.(i, a1), p.(i + 1, a1), p.(i + 1, a0)]
          end
        g.entities.add_face(pts)
      end
    end
    paint(g, m)
  end

  # ---------- объекты ----------

  def self.fountain(e, mt, cx, cy)
    cyl(e, cx, cy, 0, 8.0, 0.07, mt[:plaza], 64)                 # плитка вокруг
    ring(e, cx, cy, 0.07, 3.7, 3.2, 0.7, mt[:stone])             # борт чаши
    cyl(e, cx, cy, 0.07, 3.2, 0.02, mt[:dark], 48)               # дно
    cyl(e, cx, cy, 0.6, 3.2, 0.01, mt[:water], 48)               # вода
    cyl(e, cx, cy, 0.07, 0.6, 1.3, mt[:stone], 24)               # ножка
    ring(e, cx, cy, 1.37, 1.7, 1.4, 0.3, mt[:stone], 36)         # верхняя чаша
    cyl(e, cx, cy, 1.37, 1.4, 0.02, mt[:dark], 36)
    cyl(e, cx, cy, 1.55, 1.4, 0.01, mt[:water], 36)
    cyl(e, cx, cy, 1.4, 0.12, 1.7, mt[:stone], 12)               # шпиль
    sphere(e, cx, cy, 3.25, 0.28, mt[:water])                    # верхняя струя
    8.times do |i|                                              # боковые струи
      a = TWO_PI * i / 8
      cyl(e, cx + 2.5 * Math.cos(a), cy + 2.5 * Math.sin(a), 0.6, 0.05, 0.9, mt[:water], 8)
    end
  end

  def self.gazebo(e, mt, cx, cy)
    prism(e, ngon(cx, cy, 3.5, 6), 0, 0.3, mt[:stone])           # основание
    prism(e, ngon(cx, cy, 3.1, 6), 0.3, 0.05, mt[:wood])         # пол
    6.times do |i|                                              # колонны
      a = TWO_PI * i / 6
      cyl(e, cx + 2.85 * Math.cos(a), cy + 2.85 * Math.sin(a), 0.35, 0.13, 2.75, mt[:white], 12)
    end
    prism(e, ngon(cx, cy, 3.2, 6), 3.1, 0.18, mt[:wood])         # обвязка
    pyramid(e, cx, cy, 3.28, 4.1, 1.7, 6, mt[:roof])             # крыша
    cyl(e, cx, cy, 4.98, 0.06, 0.4, mt[:metal], 8)               # шпиль
    3.times do |i|                                              # скамьи внутри
      a = TWO_PI * (i * 2 + 1) / 6
      bx = cx + 2.2 * Math.cos(a)
      by = cy + 2.2 * Math.sin(a)
      box(e, bx - 0.6, by - 0.2, 0.55, 1.2, 0.4, 0.06, mt[:wood])
    end
  end

  def self.playground(e, mt, x0, y0)
    z0 = 0.08
    box(e, x0, y0, 0, 14, 12, z0, mt[:rubber])                   # покрытие
    # живая изгородь: юг, восток, запад (с проёмом у входа)
    box(e, x0, y0 - 0.4, 0, 14.4, 0.6, 0.8, mt[:hedge])
    box(e, x0 + 13.8, y0, 0, 0.6, 12, 0.8, mt[:hedge])
    box(e, x0 - 0.4, y0, 0, 0.6, 9, 0.8, mt[:hedge])
    # песочница
    sx = x0 + 1.5
    sy = y0 + 1
    box(e, sx, sy, z0, 3, 3, 0.35, mt[:wood])
    box(e, sx + 0.2, sy + 0.2, z0 + 0.22, 2.6, 2.6, 0.08, mt[:sand])
    # горка
    tx = x0 + 7
    ty = y0 + 7.2
    box(e, tx, ty, z0 + 1.8, 1.6, 1.6, 0.1, mt[:wood])
    [[0, 0], [1.48, 0], [0, 1.48], [1.48, 1.48]].each do |dx, dy|
      box(e, tx + dx, ty + dy, z0, 0.12, 0.12, 2.8, mt[:yellow])
    end
    pyramid(e, tx + 0.8, ty + 0.8, z0 + 2.8, 1.3, 0.7, 4, mt[:rubber], Math::PI / 4)
    prism_xz(e, [[tx + 1.6, z0 + 1.85], [tx + 4.8, z0 + 0.12],
                 [tx + 4.8, z0 + 0.32], [tx + 1.6, z0 + 2.05]], ty + 0.45, 0.7, mt[:rubber2])
    6.times { |i| box(e, tx - 0.06, ty + 0.3, z0 + 0.3 + i * 0.28, 0.06, 1.0, 0.04, mt[:metal]) }
    # качели
    wx = x0 + 8.5
    wy = y0 + 2.5
    box(e, wx, wy, z0, 0.15, 0.2, 2.5, mt[:metal])
    box(e, wx + 3.4, wy, z0, 0.15, 0.2, 2.5, mt[:metal])
    box(e, wx, wy, z0 + 2.5, 3.55, 0.2, 0.15, mt[:metal])
    [wx + 0.7, wx + 2.2].each do |sx2|
      box(e, sx2, wy - 0.1, z0 + 0.45, 0.5, 0.4, 0.06, mt[:yellow])
      box(e, sx2 + 0.02, wy + 0.09, z0 + 0.5, 0.02, 0.02, 2.0, mt[:metal])
      box(e, sx2 + 0.46, wy + 0.09, z0 + 0.5, 0.02, 0.02, 2.0, mt[:metal])
    end
    # грибок
    cyl(e, x0 + 4, y0 + 8.5, z0, 0.12, 1.0, mt[:wood], 10)
    cyl(e, x0 + 4, y0 + 8.5, z0 + 1.0, 0.7, 0.1, mt[:rubber], 16)
  end

  def self.bench(e, mt, x, y, flip = false)
    box(e, x, y, 0.42, 1.8, 0.45, 0.06, mt[:wood])
    box(e, x + 0.15, y + 0.05, 0, 0.08, 0.35, 0.42, mt[:metal])
    box(e, x + 1.57, y + 0.05, 0, 0.08, 0.35, 0.42, mt[:metal])
    by = flip ? y + 0.39 : y
    box(e, x, by, 0.5, 1.8, 0.06, 0.45, mt[:wood])
  end

  def self.lamp(e, mt, x, y)
    cyl(e, x, y, 0, 0.05, 3.5, mt[:metal], 8)
    sphere(e, x, y, 3.65, 0.2, mt[:yellow], 4, 8)
  end

  def self.tree(e, mt, rng, x, y)
    s = rng.rand(0.8..1.3)
    if rng.rand < 0.35
      cyl(e, x, y, 0, 0.15 * s, 1.2 * s, mt[:trunk], 8)
      pyramid(e, x, y, 0.8 * s, 1.5 * s, 2.6 * s, 8, mt[:conifer])
      pyramid(e, x, y, 2.2 * s, 1.1 * s, 2.2 * s, 8, mt[:conifer])
    else
      cyl(e, x, y, 0, 0.18 * s, 2.6 * s, mt[:trunk], 8)
      sphere(e, x, y, 3.6 * s, 1.9 * s, rng.rand < 0.5 ? mt[:leaf] : mt[:leaf2])
      sphere(e, x + 0.8 * s, y + 0.4 * s, 3.2 * s, 1.2 * s, mt[:leaf2])
    end
  end

  def self.flowerbed(e, mt, rng, x, y, w, d)
    box(e, x, y, 0, w, d, 0.25, mt[:soil])
    cols = [mt[:f_red], mt[:f_pink], mt[:f_white], mt[:f_yellow]]
    (0...(w / 1.2).floor).each do |i|
      (0...(d / 1.2).floor).each do |j|
        sphere(e, x + 0.6 + i * 1.2 + rng.rand(-0.15..0.15), y + 0.6 + j * 1.2 + rng.rand(-0.15..0.15),
               0.45, 0.22, cols.sample(random: rng), 3, 6)
      end
    end
  end

  def self.scene(model, name, eye, target, up = [0, 0, 1], persp = true, height = nil)
    cam = Sketchup::Camera.new(pt(*eye), pt(*target), Geom::Vector3d.new(*up), persp)
    cam.height = height.m if !persp && height
    model.active_view.camera = cam
    model.pages.add(name)
  end

  # ---------- сборка ----------

  def self.build
    model = Sketchup.active_model
    model.start_operation('Парк', true)
    begin
      m   = mats(model)
      e   = model.entities
      rng = Random.new(42)

      prism(e, [[0, 0], [60, 0], [60, 40], [0, 40]], -0.1, 0.1, m[:grass])  # газон

      # дорожки
      strip(e, [0, 20], [26, 20], 3.2, 0, 0.05, m[:path])                    # главная аллея
      strip(e, [30, 20], [47, 32], 2.5, 0, 0.05, m[:path])                   # к беседке
      strip(e, [30, 20], [41, 12.8], 2.5, 0, 0.05, m[:path])                 # к площадке
      strip(e, [34, 20], [58, 20], 2.0, 0, 0.05, m[:path])                   # восточная ветка

      fountain(e, m, 30, 20)
      gazebo(e, m, 47, 32)
      playground(e, m, 40, 2)

      # цветники и скамейки/фонари вдоль аллеи
      flowerbed(e, m, rng, 6, 6, 9, 6)
      flowerbed(e, m, rng, 6, 28, 9, 6)
      flowerbed(e, m, rng, 18, 28, 6, 5)
      [[13, 17.0], [13, 22.5]].each_with_index { |(x, y), i| bench(e, m, x, y, i == 1) }
      [[8, 17.5], [8, 22.5], [21, 17.5], [21, 22.5]].each { |x, y| lamp(e, m, x, y) }
      4.times do |i|
        a = Math::PI / 4 + i * Math::PI / 2
        lamp(e, m, 30 + 7 * Math.cos(a), 20 + 7 * Math.sin(a))
      end
      bench(e, m, 36, 24.5)
      bench(e, m, 36, 15.0, true)

      # деревья по периметру и вдоль аллеи
      (4..38).step(6) { |x| tree(e, m, rng, x, 2.5); tree(e, m, rng, x, 37.5) }
      (10..34).step(6) { |y| tree(e, m, rng, 2.5, y) }
      [2, 8, 14, 20, 26, 32, 38].each { |y| tree(e, m, rng, 57.5, y) }
      [5, 20].each { |x| tree(e, m, rng, x, 16.0); tree(e, m, rng, x, 24.0) }

      # тени, вид, сцены
      si = model.shadow_info
      si['DisplayShadows'] = true
      si['ShadowTime'] = Time.utc(2023, 7, 15, 13, 0, 0)

      scene(model, '1 Общий вид', [-22, -28, 30], [30, 20, 0])
      scene(model, '2 Вид сверху (генплан)', [30, 20, 100], [30, 20, 0], [0, 1, 0], false, 48)
      scene(model, '3 Фонтан', [14, 8, 6], [30, 20, 1.5])
      scene(model, '4 Беседка', [36, 20, 5], [47, 32, 2])
      scene(model, '5 Детская площадка', [34, 24, 9], [47, 8, 0.5])
      scene(model, '6 Вход и аллея', [-5, 14, 3], [26, 20, 1.5])
      model.active_view.zoom_extents
      model.commit_operation
      UI.messagebox('Парк построен. Сцены — вкладки сверху (или View > Animation).')
    rescue StandardError => err
      model.abort_operation
      UI.messagebox("Ошибка: #{err.message}\n#{err.backtrace.first(3).join("\n")}")
      raise
    end
  end
end

ParkGen.build
